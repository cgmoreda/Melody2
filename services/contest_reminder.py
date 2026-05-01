from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Optional, Protocol, runtime_checkable

import aiohttp
import discord

from db.repository import ReminderRepository

logger = logging.getLogger(__name__)

CF_CONTEST_LIST_API = "https://codeforces.com/api/contest.list"
REMINDER_DEDUPE_RETENTION_DAYS = 120

# ---------------------------------------------------------------------------
# Generic contest model used across all platforms
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Contest:
    """Platform-agnostic representation of an upcoming contest."""

    platform: str
    contest_id: str
    name: str
    start_time_seconds: int

    @property
    def start_time(self) -> datetime:
        return datetime.fromtimestamp(self.start_time_seconds, tz=UTC)


# Keep legacy alias so existing tests/imports keep working.
CFContest = Contest

# ---------------------------------------------------------------------------
# Contest provider protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ContestProvider(Protocol):
    """Async source of upcoming contests for a single platform."""

    @property
    def platform(self) -> str: ...

    async def fetch_upcoming(self, session: aiohttp.ClientSession) -> Optional[list[Contest]]:
        """Return upcoming contests, or *None* on transient failure."""
        ...


# ---------------------------------------------------------------------------
# Codeforces provider
# ---------------------------------------------------------------------------


class CodeforcesProvider:
    """Fetches upcoming contests from the Codeforces API."""

    @property
    def platform(self) -> str:
        return "codeforces"

    async def fetch_upcoming(self, session: aiohttp.ClientSession) -> Optional[list[Contest]]:
        delay = 1.0
        for attempt in range(1, 4):
            try:
                async with session.get(
                    CF_CONTEST_LIST_API,
                    params={"_": int(datetime.now(tz=UTC).timestamp())},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as response:
                    if response.status != 200:
                        raise RuntimeError(f"contest.list returned HTTP {response.status}")
                    payload = await response.json()
            except (aiohttp.ClientError, TimeoutError, RuntimeError, ValueError) as exc:
                logger.warning("CF contest.list attempt %d failed: %s", attempt, exc)
                if attempt < 3:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                return None

            if payload.get("status") != "OK":
                logger.warning("CF contest.list returned non-OK payload")
                if attempt < 3:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                return None

            result = payload.get("result", [])
            contests: list[Contest] = []
            for row in result:
                if row.get("phase") != "BEFORE":
                    continue
                contest_id = row.get("id")
                name = row.get("name")
                start_time_seconds = row.get("startTimeSeconds")
                if not isinstance(contest_id, int) or not isinstance(name, str) or not isinstance(start_time_seconds, int):
                    continue
                contests.append(
                    Contest(
                        platform=self.platform,
                        contest_id=str(contest_id),
                        name=name,
                        start_time_seconds=start_time_seconds,
                    )
                )
            return contests

        return None


# ---------------------------------------------------------------------------
# Reminder service
# ---------------------------------------------------------------------------

# Cache/lock key: (guild_id, channel_id, platform, contest_id, reminder_type)
_CacheKey = tuple[int, int, str, str, str]


class ContestReminderService:
    def __init__(
        self,
        *,
        session: aiohttp.ClientSession,
        bot: discord.Client,
        repo: ReminderRepository,
        providers: Sequence[ContestProvider] | None = None,
        poll_seconds: int = 300,
    ) -> None:
        self._session = session
        self._bot = bot
        self._repo = repo
        self._providers: Sequence[ContestProvider] = (
            [CodeforcesProvider()] if providers is None else providers
        )
        self._poll_seconds = max(300, min(600, poll_seconds))
        self._task: Optional[asyncio.Task[None]] = None
        self._sent_cache: set[_CacheKey] = set()
        self._dispatch_locks: dict[_CacheKey, asyncio.Lock] = {}
        self._enabled_channels: set[tuple[int, int]] = set()
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        channels = await self._repo.get_reminder_channels()
        retention_cutoff = datetime.now(tz=UTC) - timedelta(days=REMINDER_DEDUPE_RETENTION_DAYS)
        try:
            deleted = await self._repo.cleanup_old_sent_contest_reminders(retention_cutoff)
            if deleted > 0:
                logger.info("Cleaned %d old sent reminder dedupe row(s)", deleted)
        except Exception:
            logger.exception("Failed cleaning old sent reminder dedupe rows")
        async with self._lock:
            self._enabled_channels = set(channels)
        logger.info("Loaded %d reminder channel(s)", len(channels))

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run_loop(), name="contest-reminders")

    async def stop(self) -> None:
        if self._task is None:
            return

        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def enable_channel(self, guild_id: int, channel_id: int) -> bool:
        inserted = await self._repo.add_reminder_channel(guild_id, channel_id)
        async with self._lock:
            self._enabled_channels.add((guild_id, channel_id))
        return inserted

    async def disable_channel(self, guild_id: int, channel_id: int) -> bool:
        removed = await self._repo.remove_reminder_channel(guild_id, channel_id)
        async with self._lock:
            self._enabled_channels.discard((guild_id, channel_id))
            self._sent_cache = {
                cache_key
                for cache_key in self._sent_cache
                if not (cache_key[0] == guild_id and cache_key[1] == channel_id)
            }
            self._dispatch_locks = {
                cache_key: dispatch_lock
                for cache_key, dispatch_lock in self._dispatch_locks.items()
                if not (cache_key[0] == guild_id and cache_key[1] == channel_id)
            }
        return removed

    async def is_channel_enabled(self, guild_id: int, channel_id: int) -> bool:
        async with self._lock:
            return (guild_id, channel_id) in self._enabled_channels

    async def get_upcoming_contests(self, platform: str = "codeforces", limit: int = 3) -> list[Contest]:
        """Return upcoming contests for a specific platform (used by !reminder next)."""
        provider = next((p for p in self._providers if p.platform == platform), None)
        if provider is None:
            return []
        contests = await provider.fetch_upcoming(self._session)
        if contests is None:
            return []

        if platform == "codeforces":
            contests = [contest for contest in contests if _is_auto_reminder_contest(contest)]

        contests.sort(key=lambda contest: contest.start_time_seconds)
        return contests[: max(1, limit)]

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        logger.info("Contest reminder loop started (poll=%ss)", self._poll_seconds)
        while True:
            try:
                await self._tick()
            except Exception:
                logger.exception("Contest reminder tick failed")
            await asyncio.sleep(self._poll_seconds)

    async def _tick(self) -> None:
        all_contests: list[Contest] = []
        for provider in self._providers:
            try:
                contests = await provider.fetch_upcoming(self._session)
            except Exception:
                logger.exception("Provider %s fetch failed", provider.platform)
                continue
            if contests is not None:
                all_contests.extend(contests)

        all_contests = [contest for contest in all_contests if _is_auto_reminder_contest(contest)]
        if not all_contests:
            return

        now = datetime.now(tz=UTC)
        poll_window = timedelta(seconds=self._poll_seconds)
        active_keys = {(c.platform, c.contest_id) for c in all_contests}

        async with self._lock:
            enabled_channels = list(self._enabled_channels)
            self._sent_cache = {
                cache_key
                for cache_key in self._sent_cache
                if (cache_key[2], cache_key[3]) in active_keys
            }
            self._dispatch_locks = {
                cache_key: dispatch_lock
                for cache_key, dispatch_lock in self._dispatch_locks.items()
                if (cache_key[2], cache_key[3]) in active_keys
            }

        for guild_id, channel_id in enabled_channels:
            channel = self._bot.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                logger.warning("Reminder channel %s in guild %s is not accessible", channel_id, guild_id)
                continue

            for contest in all_contests:
                until_start = contest.start_time - now
                if until_start <= timedelta(seconds=0):
                    continue

                await self._maybe_send_reminder(
                    guild_id=guild_id,
                    channel=channel,
                    contest=contest,
                    reminder_type="24h",
                    target=timedelta(hours=24),
                    window=poll_window,
                    message=f"[Reminder] {contest.name} starts in 24h",
                    until_start=until_start,
                )
                await self._maybe_send_reminder(
                    guild_id=guild_id,
                    channel=channel,
                    contest=contest,
                    reminder_type="1h",
                    target=timedelta(hours=1),
                    window=poll_window,
                    message=f"[Reminder] {contest.name} starts in 1h",
                    until_start=until_start,
                )

    # ------------------------------------------------------------------
    # Reminder dispatch
    # ------------------------------------------------------------------

    async def _maybe_send_reminder(
        self,
        *,
        guild_id: int,
        channel: discord.TextChannel,
        contest: Contest,
        reminder_type: str,
        target: timedelta,
        window: timedelta,
        message: str,
        until_start: timedelta,
    ) -> None:
        key: _CacheKey = (guild_id, channel.id, contest.platform, contest.contest_id, reminder_type)
        async with self._lock:
            if key in self._sent_cache:
                return

        if not (target - window <= until_start <= target):
            return

        dispatch_lock = await self._get_dispatch_lock(key)
        async with dispatch_lock:
            async with self._lock:
                if key in self._sent_cache:
                    return

            try:
                already_sent = await self._repo.has_sent_contest_reminder(
                    guild_id=guild_id,
                    channel_id=channel.id,
                    contest_id=contest.contest_id,
                    reminder_type=reminder_type,
                    platform=contest.platform,
                )
            except Exception:
                logger.exception(
                    "Failed reading reminder dedupe key (guild=%s channel=%s platform=%s contest=%s type=%s)",
                    guild_id,
                    channel.id,
                    contest.platform,
                    contest.contest_id,
                    reminder_type,
                )
                return

            if already_sent:
                async with self._lock:
                    self._sent_cache.add(key)
                return

            try:
                await channel.send(message)
            except discord.Forbidden:
                logger.warning(
                    "Cannot send reminder to channel %s in guild %s due to missing permissions",
                    channel.id,
                    guild_id,
                )
                return
            except discord.HTTPException as exc:
                logger.warning(
                    "Failed sending reminder to channel %s in guild %s: %s",
                    channel.id,
                    guild_id,
                    exc,
                )
                return

            sent_at = datetime.now(tz=UTC)
            try:
                await self._mark_sent_with_retry(
                    guild_id=guild_id,
                    channel_id=channel.id,
                    contest_id=contest.contest_id,
                    reminder_type=reminder_type,
                    sent_at=sent_at,
                    platform=contest.platform,
                )
            except Exception:
                logger.exception(
                    "Sent reminder but failed to persist dedupe row (guild=%s channel=%s platform=%s contest=%s type=%s)",
                    guild_id,
                    channel.id,
                    contest.platform,
                    contest.contest_id,
                    reminder_type,
                )

            async with self._lock:
                self._sent_cache.add(key)

            logger.info(
                "Sent %s reminder for %s contest %s (%s) in guild %s channel %s",
                reminder_type,
                contest.platform,
                contest.contest_id,
                contest.name,
                guild_id,
                channel.id,
            )

    async def _get_dispatch_lock(self, key: _CacheKey) -> asyncio.Lock:
        async with self._lock:
            lock = self._dispatch_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._dispatch_locks[key] = lock
            return lock

    async def _mark_sent_with_retry(
        self,
        *,
        guild_id: int,
        channel_id: int,
        contest_id: str,
        reminder_type: str,
        sent_at: datetime,
        platform: str,
    ) -> None:
        delay_seconds = 0.25
        for attempt in range(1, 4):
            try:
                await self._repo.mark_contest_reminder_sent(
                    guild_id=guild_id,
                    channel_id=channel_id,
                    contest_id=contest_id,
                    reminder_type=reminder_type,
                    sent_at=sent_at,
                    platform=platform,
                )
                return
            except Exception:
                if attempt == 3:
                    raise
                await asyncio.sleep(delay_seconds)
                delay_seconds *= 2.0


def _is_div_contest_name(name: str) -> bool:
    lowered = name.lower()
    return "div." in lowered or "div " in lowered


def _is_auto_reminder_contest(contest: Contest) -> bool:
    if contest.platform != "codeforces":
        return True
    return _is_div_contest_name(contest.name)
