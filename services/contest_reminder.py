from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Optional

import aiohttp
import discord

from db.repository import UserRepositoryBase

logger = logging.getLogger(__name__)

CF_CONTEST_LIST_API = "https://codeforces.com/api/contest.list"


@dataclass(frozen=True, slots=True)
class CFContest:
    contest_id: int
    name: str
    start_time_seconds: int

    @property
    def start_time(self) -> datetime:
        return datetime.fromtimestamp(self.start_time_seconds, tz=UTC)


class ContestReminderService:
    def __init__(
        self,
        *,
        session: aiohttp.ClientSession,
        bot: discord.Client,
        repo: UserRepositoryBase,
        poll_seconds: int = 300,
    ) -> None:
        self._session = session
        self._bot = bot
        self._repo = repo
        self._poll_seconds = max(300, min(600, poll_seconds))
        self._task: Optional[asyncio.Task[None]] = None
        self._sent_cache: set[tuple[int, int, str]] = set()
        self._enabled_channels: set[tuple[int, int]] = set()
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        channels = await self._repo.get_reminder_channels()
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

    async def _run_loop(self) -> None:
        logger.info("Contest reminder loop started (poll=%ss)", self._poll_seconds)
        while True:
            try:
                await self._tick()
            except Exception:
                logger.exception("Contest reminder tick failed")
            await asyncio.sleep(self._poll_seconds)

    async def _tick(self) -> None:
        contests = await self._fetch_before_contests()
        if contests is None:
            return

        now = datetime.now(tz=UTC)
        poll_window = timedelta(seconds=self._poll_seconds)
        contest_ids = {c.contest_id for c in contests}
        async with self._lock:
            enabled_channels = list(self._enabled_channels)
            self._sent_cache = {
                key for key in self._sent_cache if key[1] in contest_ids
            }

        for guild_id, channel_id in enabled_channels:
            channel = self._bot.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                logger.warning("Reminder channel %s in guild %s is not accessible", channel_id, guild_id)
                continue

            for contest in contests:
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
                    message=f"📢 {contest.name} starts in 24h",
                    until_start=until_start,
                )
                await self._maybe_send_reminder(
                    guild_id=guild_id,
                    channel=channel,
                    contest=contest,
                    reminder_type="1h",
                    target=timedelta(hours=1),
                    window=poll_window,
                    message=f"⏰ {contest.name} starts in 1h",
                    until_start=until_start,
                )

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
                key for key in self._sent_cache if not (key[0] == channel_id and key[2] in {"24h", "1h"})
            }
        return removed

    async def is_channel_enabled(self, guild_id: int, channel_id: int) -> bool:
        async with self._lock:
            return (guild_id, channel_id) in self._enabled_channels

    async def _maybe_send_reminder(
        self,
        *,
        guild_id: int,
        channel: discord.TextChannel,
        contest: CFContest,
        reminder_type: str,
        target: timedelta,
        window: timedelta,
        message: str,
        until_start: timedelta,
    ) -> None:
        key = (channel.id, contest.contest_id, reminder_type)
        async with self._lock:
            if key in self._sent_cache:
                return
        if not (target - window <= until_start <= target):
            return

        await channel.send(message)
        async with self._lock:
            self._sent_cache.add(key)
        logger.info(
            "Sent %s reminder for contest %s (%s) in guild %s channel %s",
            reminder_type,
            contest.contest_id,
            contest.name,
            guild_id,
            channel.id,
        )

    async def _fetch_before_contests(self) -> Optional[list[CFContest]]:
        delay = 1.0
        for attempt in range(1, 4):
            try:
                async with self._session.get(
                    CF_CONTEST_LIST_API,
                    params={"_": int(datetime.now(tz=UTC).timestamp())},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"contest.list returned HTTP {resp.status}")
                    payload = await resp.json()
            except (aiohttp.ClientError, TimeoutError, RuntimeError) as exc:
                logger.warning("contest.list attempt %d failed: %s", attempt, exc)
                if attempt < 3:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                return None

            if payload.get("status") != "OK":
                logger.warning("contest.list returned non-OK payload")
                if attempt < 3:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                return None

            result = payload.get("result", [])
            contests: list[CFContest] = []
            for row in result:
                if row.get("phase") != "BEFORE":
                    continue
                contest_id = row.get("id")
                name = row.get("name")
                start_time_seconds = row.get("startTimeSeconds")
                if not isinstance(contest_id, int) or not isinstance(name, str) or not isinstance(start_time_seconds, int):
                    continue
                contests.append(
                    CFContest(
                        contest_id=contest_id,
                        name=name,
                        start_time_seconds=start_time_seconds,
                    )
                )
            return contests

        return None
