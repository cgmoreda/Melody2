"""Verification Manager for scheduling AFK checks across dynamic voice channels."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional, Protocol

logger = logging.getLogger(__name__)

# ── Channel type display names ──────────────────────────────────────

_CHANNEL_TYPE_DISPLAY: dict[str, str] = {
    "solo": "Solo",
    "duo": "Duo",
    "team": "Team",
    "invite": "Invite",
}


def channel_type_display_name(channel_type: str) -> str:
    """Return a user-facing display name for a channel type."""
    return _CHANNEL_TYPE_DISPLAY.get(channel_type, channel_type.title())


# ── Protocols ───────────────────────────────────────────────────────

class VerificationCallback(Protocol):
    async def __call__(
        self,
        guild_id: int,
        member_id: int,
        channel_id: int,
        channel_type: str,
    ) -> bool:
        """Called when a verification check is due.

        Must return True if the check passed (and should be rescheduled),
        or False if it failed/ended (and should not be rescheduled).
        """
        ...


class RandomGenerator(Protocol):
    def __call__(self, min_val: int, max_val: int) -> int:
        ...


class IntervalProvider(Protocol):
    async def __call__(self, guild_id: int, channel_type: str) -> tuple[int, int]:
        ...


# ── Session key type ────────────────────────────────────────────────

SessionKey = tuple[int, int]  # (guild_id, member_id)


# ── Data classes ────────────────────────────────────────────────────

@dataclass
class VerificationSession:
    """Represents an active verification timer for a specific member in a channel."""

    member_id: int
    guild_id: int
    channel_id: int
    channel_type: str
    created_at: datetime
    generation: int
    task: Optional[asyncio.Task[None]] = None

    @property
    def key(self) -> SessionKey:
        return (self.guild_id, self.member_id)


@dataclass
class Metrics:
    """Lightweight metrics for the VerificationManager."""

    scheduled: int = 0
    cancelled: int = 0
    completed: int = 0
    verification_passed: int = 0
    verification_failed: int = 0


class VerificationManager:
    """Manages verification scheduling based on channel occupancy."""

    def __init__(
        self,
        get_interval: IntervalProvider,
        on_verification_due: VerificationCallback,
        random_fn: RandomGenerator,
    ) -> None:
        self._get_interval = get_interval
        self._on_verification_due = on_verification_due
        self._random_fn = random_fn
        self._sessions: dict[SessionKey, VerificationSession] = {}
        self._generation_counter = 0
        self.metrics = Metrics()

    @staticmethod
    def _key(guild_id: int, member_id: int) -> SessionKey:
        return (guild_id, member_id)

    def _next_generation(self) -> int:
        self._generation_counter += 1
        return self._generation_counter

    def is_scheduled(self, guild_id: int, member_id: int) -> bool:
        """Check if a member currently has a verification session."""
        return self._key(guild_id, member_id) in self._sessions

    def cancel(self, guild_id: int, member_id: int) -> None:
        """Cancel the pending verification session for a member."""
        key = self._key(guild_id, member_id)
        session = self._sessions.pop(key, None)
        if session:
            if session.task and not session.task.done():
                session.task.cancel()
            self.metrics.cancelled += 1
            logger.debug(
                "Cancelled verification for member %s in guild %s channel %s.",
                member_id, guild_id, session.channel_id,
            )

    def cancel_for_channel(self, channel_id: int) -> None:
        """Cancel verification sessions for all members in a given channel."""
        keys = [
            key
            for key, session in self._sessions.items()
            if session.channel_id == channel_id
        ]
        for guild_id, member_id in keys:
            self.cancel(guild_id, member_id)

    def shutdown(self) -> None:
        """Cancel all pending verification sessions."""
        for guild_id, member_id in list(self._sessions.keys()):
            self.cancel(guild_id, member_id)

    async def evaluate_channel(
        self,
        guild_id: int,
        channel_id: int,
        channel_type: str,
        human_member_ids: list[int],
    ) -> None:
        """Evaluate occupancy and schedule or cancel verification sessions.

        Args:
            guild_id: The ID of the guild.
            channel_id: The ID of the channel being evaluated.
            channel_type: The type of the channel (e.g., 'solo', 'duo').
            human_member_ids: A list of non-bot member IDs currently in the channel.
        """
        num_humans = len(human_member_ids)

        if channel_type == "solo":
            should_schedule = num_humans > 0
        else:
            should_schedule = num_humans == 1

        if not should_schedule:
            self.cancel_for_channel(channel_id)
            return

        for member_id in human_member_ids:
            key = self._key(guild_id, member_id)
            # Idempotency check: if already scheduled for this specific channel, do nothing
            existing = self._sessions.get(key)
            if existing and existing.channel_id == channel_id and existing.channel_type == channel_type:
                continue

            # If they were in another channel previously and moved, cancel old session
            if existing:
                self.cancel(guild_id, member_id)

            await self._schedule(guild_id, member_id, channel_id, channel_type)

    async def _schedule(
        self,
        guild_id: int,
        member_id: int,
        channel_id: int,
        channel_type: str,
    ) -> None:
        """Create a new verification session and schedule the timer task."""
        min_sec, max_sec = await self._get_interval(guild_id, channel_type)
        delay = self._random_fn(min_sec, max_sec)

        generation = self._next_generation()
        session = VerificationSession(
            member_id=member_id,
            guild_id=guild_id,
            channel_id=channel_id,
            channel_type=channel_type,
            created_at=datetime.now(timezone.utc),
            generation=generation,
        )

        key = self._key(guild_id, member_id)
        self._sessions[key] = session
        session.task = asyncio.create_task(
            self._timer_loop(session, delay)
        )
        self.metrics.scheduled += 1
        logger.debug(
            "Scheduled verification for member %s in guild %s in %ss (gen %s).",
            member_id, guild_id, delay, generation,
        )

    async def _timer_loop(self, session: VerificationSession, delay: int) -> None:
        """Wait for the delay, then invoke the callback."""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        # Check generation token to prevent race conditions
        key = session.key
        current = self._sessions.get(key)
        if current is None or current.generation != session.generation:
            logger.debug("Timer for member %s aborted due to generation mismatch.", session.member_id)
            return

        self.metrics.completed += 1

        try:
            # We must await the callback. The cog handles moving, UI, etc.
            passed = await self._on_verification_due(
                session.guild_id,
                session.member_id,
                session.channel_id,
                session.channel_type,
            )
        except Exception:
            logger.exception("Verification callback failed for member %s.", session.member_id)
            self.cancel(session.guild_id, session.member_id)
            return

        # Recheck token in case it was cancelled while awaiting callback
        current = self._sessions.get(key)
        if current is None or current.generation != session.generation:
            return

        if passed:
            self.metrics.verification_passed += 1
            # Verification successful, schedule next check
            await self._schedule(
                session.guild_id,
                session.member_id,
                session.channel_id,
                session.channel_type,
            )
        else:
            self.metrics.verification_failed += 1
            # Verification failed, end session
            self.cancel(session.guild_id, session.member_id)
