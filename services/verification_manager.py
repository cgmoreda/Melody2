"""Verification Manager for scheduling AFK checks across dynamic voice channels."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional, Protocol

logger = logging.getLogger(__name__)


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
        self._sessions: dict[int, VerificationSession] = {}
        self._generation_counter = 0
        self.metrics = Metrics()

    def _next_generation(self) -> int:
        self._generation_counter += 1
        return self._generation_counter

    def is_scheduled(self, member_id: int) -> bool:
        """Check if a member currently has a verification session."""
        return member_id in self._sessions

    def cancel(self, member_id: int) -> None:
        """Cancel the pending verification session for a member."""
        session = self._sessions.pop(member_id, None)
        if session:
            if session.task and not session.task.done():
                session.task.cancel()
            self.metrics.cancelled += 1
            logger.debug("Cancelled verification for member %s in channel %s.", member_id, session.channel_id)

    def cancel_for_channel(self, channel_id: int) -> None:
        """Cancel verification sessions for all members in a given channel."""
        member_ids = [
            member_id
            for member_id, session in self._sessions.items()
            if session.channel_id == channel_id
        ]
        for member_id in member_ids:
            self.cancel(member_id)

    def shutdown(self) -> None:
        """Cancel all pending verification sessions."""
        for member_id in list(self._sessions.keys()):
            self.cancel(member_id)

    async def evaluate_channel(
        self,
        guild_id: int,
        channel_id: int,
        channel_type: str,
        human_member_ids: list[int],
        expected_capacity: int = 1,
    ) -> None:
        """Evaluate occupancy and schedule or cancel verification sessions.
        
        Args:
            guild_id: The ID of the guild.
            channel_id: The ID of the channel being evaluated.
            channel_type: The type of the channel (e.g., 'solo', 'duo').
            human_member_ids: A list of non-bot member IDs currently in the channel.
            expected_capacity: The expected capacity of the channel (used for 'invite' channels).
        """
        num_humans = len(human_member_ids)
        
        if channel_type == "solo":
            should_schedule = num_humans > 0
        elif channel_type == "invite":
            should_schedule = num_humans > 0 and num_humans < expected_capacity
        else:
            should_schedule = num_humans == 1

        if not should_schedule:
            self.cancel_for_channel(channel_id)
            return

        for member_id in human_member_ids:
            # Idempotency check: if already scheduled for this specific channel, do nothing
            existing = self._sessions.get(member_id)
            if existing and existing.channel_id == channel_id and existing.channel_type == channel_type:
                continue

            # If they were in another channel previously and moved, cancel old session
            if existing:
                self.cancel(member_id)

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
        
        self._sessions[member_id] = session
        session.task = asyncio.create_task(
            self._timer_loop(session, delay)
        )
        self.metrics.scheduled += 1
        logger.debug("Scheduled verification for member %s in %ss (gen %s).", member_id, delay, generation)

    async def _timer_loop(self, session: VerificationSession, delay: int) -> None:
        """Wait for the delay, then invoke the callback."""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        # Check generation token to prevent race conditions
        current = self._sessions.get(session.member_id)
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
            self.cancel(session.member_id)
            return

        # Recheck token in case it was cancelled while awaiting callback
        current = self._sessions.get(session.member_id)
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
            self.cancel(session.member_id)
