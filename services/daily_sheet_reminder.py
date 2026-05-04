from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Optional

import discord

from db.repository import DailySheetReminderConfig, DailySheetReminderRepository
from services.discord_output import DISCORD_MESSAGE_CHAR_LIMIT

logger = logging.getLogger(__name__)

DEFAULT_DAILY_SHEET_MESSAGE = "Reminder: please update your daily sheets."
DEFAULT_DAILY_SHEET_POLL_SECONDS = 60


def parse_utc_time(raw: str) -> tuple[int, int]:
    value = raw.strip()
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError("Time must use `HH:MM` in UTC, for example `20:30`.")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise ValueError("Time must use numbers in `HH:MM` format.") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("Time must be between `00:00` and `23:59` UTC.")
    return hour, minute


def format_utc_time(hour: int, minute: int) -> str:
    return f"{hour:02d}:{minute:02d} UTC"


class DailySheetReminderService:
    def __init__(
        self,
        *,
        bot: discord.Client,
        repo: DailySheetReminderRepository,
        poll_seconds: int = DEFAULT_DAILY_SHEET_POLL_SECONDS,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._bot = bot
        self._repo = repo
        self._poll_seconds = max(30, poll_seconds)
        self._now = now_provider or (lambda: datetime.now(tz=UTC))
        self._task: Optional[asyncio.Task[None]] = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run_loop(), name="daily-sheet-reminders")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def set_reminder(
        self,
        *,
        guild_id: int,
        channel_id: int,
        remind_hour_utc: int,
        remind_minute_utc: int,
        message: str,
    ) -> DailySheetReminderConfig:
        clean_message = message.strip() or DEFAULT_DAILY_SHEET_MESSAGE
        if len(clean_message) > DISCORD_MESSAGE_CHAR_LIMIT:
            raise ValueError(f"Reminder message must be {DISCORD_MESSAGE_CHAR_LIMIT} characters or fewer.")
        await self._repo.upsert_daily_sheet_reminder(
            guild_id=guild_id,
            channel_id=channel_id,
            remind_hour_utc=remind_hour_utc,
            remind_minute_utc=remind_minute_utc,
            message=clean_message,
        )
        return DailySheetReminderConfig(
            guild_id=guild_id,
            channel_id=channel_id,
            remind_hour_utc=remind_hour_utc,
            remind_minute_utc=remind_minute_utc,
            message=clean_message,
            last_sent_on=None,
        )

    async def disable_reminder(self, guild_id: int) -> bool:
        return await self._repo.delete_daily_sheet_reminder(guild_id)

    async def get_reminder(self, guild_id: int) -> Optional[DailySheetReminderConfig]:
        return await self._repo.get_daily_sheet_reminder(guild_id)

    async def _run_loop(self) -> None:
        logger.info("Daily sheet reminder loop started (poll=%ss)", self._poll_seconds)
        while True:
            try:
                await self._tick()
            except Exception:
                logger.exception("Daily sheet reminder tick failed")
            await asyncio.sleep(self._poll_seconds)

    async def _tick(self) -> None:
        now = self._now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        else:
            now = now.astimezone(UTC)

        reminders = await self._repo.list_daily_sheet_reminders()
        for reminder in reminders:
            if not self._is_due(reminder, now):
                continue
            await self._send_due_reminder(reminder, now)

    @staticmethod
    def _is_due(reminder: DailySheetReminderConfig, now: datetime) -> bool:
        today = now.date()
        if reminder.last_sent_on is not None and reminder.last_sent_on >= today:
            return False
        current_minutes = now.hour * 60 + now.minute
        target_minutes = reminder.remind_hour_utc * 60 + reminder.remind_minute_utc
        return current_minutes >= target_minutes

    async def _send_due_reminder(self, reminder: DailySheetReminderConfig, now: datetime) -> None:
        channel = self._bot.get_channel(reminder.channel_id)
        if not isinstance(channel, discord.TextChannel):
            logger.warning(
                "Daily sheet reminder channel %s in guild %s is not accessible",
                reminder.channel_id,
                reminder.guild_id,
            )
            return

        try:
            await channel.send(reminder.message)
        except discord.Forbidden:
            logger.warning(
                "Cannot send daily sheet reminder to channel %s in guild %s due to missing permissions",
                reminder.channel_id,
                reminder.guild_id,
            )
            return
        except discord.HTTPException as exc:
            logger.warning(
                "Failed sending daily sheet reminder to channel %s in guild %s: %s",
                reminder.channel_id,
                reminder.guild_id,
                exc,
            )
            return

        marked = await self._repo.mark_daily_sheet_reminder_sent(reminder.guild_id, now.date())
        if marked:
            logger.info(
                "Sent daily sheet reminder in guild %s channel %s",
                reminder.guild_id,
                reminder.channel_id,
            )
