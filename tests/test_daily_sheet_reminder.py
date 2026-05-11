from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Optional

import pytest

import cogs.daily_sheets as daily_sheets_cog_module
from cogs.daily_sheets import DailySheetsCog
from db.repository import DailySheetReminderConfig
import services.daily_sheet_reminder as daily_sheet_module
from services.daily_sheet_reminder import DailySheetReminderService, parse_utc_time
from services.discord_output import DISCORD_MESSAGE_CHAR_LIMIT


class _FakeRepo:
    def __init__(self, configs: list[DailySheetReminderConfig] | None = None) -> None:
        self.configs = configs or []
        self.marked: list[tuple[int, date]] = []
        self.cleared: list[tuple[int, date]] = []
        self.upserts: list[dict[str, object]] = []
        self.deleted: list[int] = []

    async def list_daily_sheet_reminders(self) -> list[DailySheetReminderConfig]:
        return list(self.configs)

    async def mark_daily_sheet_reminder_sent(self, guild_id: int, sent_on: date) -> bool:
        self.marked.append((guild_id, sent_on))
        for index, config in enumerate(self.configs):
            if config.guild_id != guild_id:
                continue
            if config.last_sent_on is not None and config.last_sent_on >= sent_on:
                return False
            self.configs[index] = DailySheetReminderConfig(
                guild_id=config.guild_id,
                channel_id=config.channel_id,
                remind_hour_utc=config.remind_hour_utc,
                remind_minute_utc=config.remind_minute_utc,
                message=config.message,
                last_sent_on=sent_on,
            )
            return True
        return False

    async def clear_daily_sheet_reminder_sent(self, guild_id: int, sent_on: date) -> bool:
        self.cleared.append((guild_id, sent_on))
        for index, config in enumerate(self.configs):
            if config.guild_id != guild_id or config.last_sent_on != sent_on:
                continue
            self.configs[index] = DailySheetReminderConfig(
                guild_id=config.guild_id,
                channel_id=config.channel_id,
                remind_hour_utc=config.remind_hour_utc,
                remind_minute_utc=config.remind_minute_utc,
                message=config.message,
                last_sent_on=None,
            )
            return True
        return False

    async def upsert_daily_sheet_reminder(
        self,
        *,
        guild_id: int,
        channel_id: int,
        remind_hour_utc: int,
        remind_minute_utc: int,
        message: str,
    ) -> None:
        self.upserts.append(
            {
                "guild_id": guild_id,
                "channel_id": channel_id,
                "remind_hour_utc": remind_hour_utc,
                "remind_minute_utc": remind_minute_utc,
                "message": message,
            }
        )

    async def delete_daily_sheet_reminder(self, guild_id: int) -> bool:
        self.deleted.append(guild_id)
        before = len(self.configs)
        self.configs = [config for config in self.configs if config.guild_id != guild_id]
        return len(self.configs) != before

    async def get_daily_sheet_reminder(self, guild_id: int) -> Optional[DailySheetReminderConfig]:
        return next((config for config in self.configs if config.guild_id == guild_id), None)


class _FakeTextChannel:
    def __init__(self, channel_id: int, name: str = "daily") -> None:
        self.id = channel_id
        self.name = name
        self.mention = f"#{name}"
        self.sent: list[str] = []
        self.sent_kwargs: list[dict[str, object]] = []

    async def send(self, message: str, **kwargs: object) -> None:
        self.sent.append(message)
        self.sent_kwargs.append(kwargs)


class _FakeBot:
    def __init__(self, channels: dict[int, _FakeTextChannel]) -> None:
        self._channels = channels

    def get_channel(self, channel_id: int) -> Optional[_FakeTextChannel]:
        return self._channels.get(channel_id)


class _FakeGuild:
    id = 1

    def __init__(self, channels: list[_FakeTextChannel]) -> None:
        self.text_channels = channels
        self._channels = {channel.id: channel for channel in channels}

    def get_channel(self, channel_id: int) -> Optional[_FakeTextChannel]:
        return self._channels.get(channel_id)


class _FakeContext:
    def __init__(self, guild: _FakeGuild, channel: _FakeTextChannel) -> None:
        self.guild = guild
        self.channel = channel
        self.sent: list[str] = []

    async def send(self, message: str, **kwargs: object) -> None:
        self.sent.append(message)


def test_parse_utc_time_accepts_hh_mm_and_rejects_invalid_values() -> None:
    assert parse_utc_time("00:00") == (0, 0)
    assert parse_utc_time("23:59") == (23, 59)

    with pytest.raises(ValueError):
        parse_utc_time("24:00")
    with pytest.raises(ValueError):
        parse_utc_time("7pm")


@pytest.mark.asyncio
async def test_daily_sheet_reminder_tick_sends_due_reminder_once_per_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daily_sheet_module.discord, "TextChannel", _FakeTextChannel)
    channel = _FakeTextChannel(99)
    now = datetime(2026, 5, 4, 20, 31, tzinfo=UTC)
    repo = _FakeRepo(
        [
            DailySheetReminderConfig(
                guild_id=1,
                channel_id=99,
                remind_hour_utc=20,
                remind_minute_utc=30,
                message="Update daily sheets.",
                last_sent_on=None,
            )
        ]
    )
    service = DailySheetReminderService(
        bot=_FakeBot({99: channel}),  # type: ignore[arg-type]
        repo=repo,
        now_provider=lambda: now,
    )

    await service._tick()
    await service._tick()

    assert channel.sent == ["@everyone Update daily sheets."]
    assert channel.sent_kwargs[0]["allowed_mentions"].to_dict() == {"parse": ["everyone"]}
    assert repo.marked == [(1, date(2026, 5, 4))]
    assert repo.cleared == []


@pytest.mark.asyncio
async def test_daily_sheet_reminder_claims_before_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daily_sheet_module.discord, "TextChannel", _FakeTextChannel)
    now = datetime(2026, 5, 4, 20, 31, tzinfo=UTC)
    repo = _FakeRepo(
        [
            DailySheetReminderConfig(
                guild_id=1,
                channel_id=99,
                remind_hour_utc=20,
                remind_minute_utc=30,
                message="Update daily sheets.",
                last_sent_on=None,
            )
        ]
    )

    class _AssertingTextChannel(_FakeTextChannel):
        async def send(self, message: str, **kwargs: object) -> None:
            assert repo.configs[0].last_sent_on == date(2026, 5, 4)
            await super().send(message, **kwargs)

    channel = _AssertingTextChannel(99)
    service = DailySheetReminderService(
        bot=_FakeBot({99: channel}),  # type: ignore[arg-type]
        repo=repo,
        now_provider=lambda: now,
    )

    await service._tick()

    assert channel.sent == ["@everyone Update daily sheets."]


@pytest.mark.asyncio
async def test_daily_sheet_reminder_does_not_duplicate_everyone_mention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daily_sheet_module.discord, "TextChannel", _FakeTextChannel)
    channel = _FakeTextChannel(99)
    now = datetime(2026, 5, 4, 20, 31, tzinfo=UTC)
    repo = _FakeRepo(
        [
            DailySheetReminderConfig(
                guild_id=1,
                channel_id=99,
                remind_hour_utc=20,
                remind_minute_utc=30,
                message="@everyone Update daily sheets.",
                last_sent_on=None,
            )
        ]
    )
    service = DailySheetReminderService(
        bot=_FakeBot({99: channel}),  # type: ignore[arg-type]
        repo=repo,
        now_provider=lambda: now,
    )

    await service._tick()

    assert channel.sent == ["@everyone Update daily sheets."]


@pytest.mark.asyncio
async def test_daily_sheet_reminder_clears_claim_on_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Forbidden(Exception):
        pass

    class _FailingTextChannel(_FakeTextChannel):
        async def send(self, message: str, **kwargs: object) -> None:
            raise _Forbidden()

    monkeypatch.setattr(daily_sheet_module.discord, "TextChannel", _FakeTextChannel)
    monkeypatch.setattr(daily_sheet_module.discord, "Forbidden", _Forbidden)
    channel = _FailingTextChannel(99)
    now = datetime(2026, 5, 4, 20, 31, tzinfo=UTC)
    repo = _FakeRepo(
        [
            DailySheetReminderConfig(
                guild_id=1,
                channel_id=99,
                remind_hour_utc=20,
                remind_minute_utc=30,
                message="Update daily sheets.",
                last_sent_on=None,
            )
        ]
    )
    service = DailySheetReminderService(
        bot=_FakeBot({99: channel}),  # type: ignore[arg-type]
        repo=repo,
        now_provider=lambda: now,
    )

    await service._tick()

    assert repo.marked == [(1, date(2026, 5, 4))]
    assert repo.cleared == [(1, date(2026, 5, 4))]
    assert repo.configs[0].last_sent_on is None


@pytest.mark.asyncio
async def test_daily_sheet_reminder_keeps_claim_on_ambiguous_http_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Forbidden(Exception):
        pass

    class _HTTPException(Exception):
        pass

    class _FailingTextChannel(_FakeTextChannel):
        async def send(self, message: str, **kwargs: object) -> None:
            raise _HTTPException("unknown delivery state")

    monkeypatch.setattr(daily_sheet_module.discord, "TextChannel", _FakeTextChannel)
    monkeypatch.setattr(daily_sheet_module.discord, "Forbidden", _Forbidden)
    monkeypatch.setattr(daily_sheet_module.discord, "HTTPException", _HTTPException)
    channel = _FailingTextChannel(99)
    now = datetime(2026, 5, 4, 20, 31, tzinfo=UTC)
    repo = _FakeRepo(
        [
            DailySheetReminderConfig(
                guild_id=1,
                channel_id=99,
                remind_hour_utc=20,
                remind_minute_utc=30,
                message="Update daily sheets.",
                last_sent_on=None,
            )
        ]
    )
    service = DailySheetReminderService(
        bot=_FakeBot({99: channel}),  # type: ignore[arg-type]
        repo=repo,
        now_provider=lambda: now,
    )

    await service._tick()

    assert repo.marked == [(1, date(2026, 5, 4))]
    assert repo.cleared == []
    assert repo.configs[0].last_sent_on == date(2026, 5, 4)


@pytest.mark.asyncio
async def test_daily_sheet_reminder_tick_waits_until_configured_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daily_sheet_module.discord, "TextChannel", _FakeTextChannel)
    channel = _FakeTextChannel(99)
    repo = _FakeRepo(
        [
            DailySheetReminderConfig(
                guild_id=1,
                channel_id=99,
                remind_hour_utc=20,
                remind_minute_utc=30,
                message="Update daily sheets.",
                last_sent_on=None,
            )
        ]
    )
    service = DailySheetReminderService(
        bot=_FakeBot({99: channel}),  # type: ignore[arg-type]
        repo=repo,
        now_provider=lambda: datetime(2026, 5, 4, 20, 29, tzinfo=UTC),
    )

    await service._tick()

    assert channel.sent == []
    assert repo.marked == []


@pytest.mark.asyncio
async def test_set_reminder_persists_clean_message() -> None:
    repo = _FakeRepo()
    service = DailySheetReminderService(bot=_FakeBot({}), repo=repo)  # type: ignore[arg-type]

    config = await service.set_reminder(
        guild_id=1,
        channel_id=2,
        remind_hour_utc=9,
        remind_minute_utc=5,
        message="  Update the sheet.  ",
    )

    assert config.message == "Update the sheet."
    assert repo.upserts == [
        {
            "guild_id": 1,
            "channel_id": 2,
            "remind_hour_utc": 9,
            "remind_minute_utc": 5,
            "message": "Update the sheet.",
        }
    ]


@pytest.mark.asyncio
async def test_set_reminder_rejects_message_too_long_after_everyone_prefix() -> None:
    repo = _FakeRepo()
    service = DailySheetReminderService(bot=_FakeBot({}), repo=repo)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="including the @everyone mention"):
        await service.set_reminder(
            guild_id=1,
            channel_id=2,
            remind_hour_utc=9,
            remind_minute_utc=5,
            message="x" * DISCORD_MESSAGE_CHAR_LIMIT,
        )

    assert repo.upserts == []


@pytest.mark.asyncio
async def test_dailysheets_set_accepts_channel_before_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daily_sheets_cog_module.discord, "TextChannel", _FakeTextChannel)
    repo = _FakeRepo()
    service = DailySheetReminderService(bot=_FakeBot({}), repo=repo)  # type: ignore[arg-type]
    cog = DailySheetsCog(service)
    default_channel = _FakeTextChannel(1, "general")
    target_channel = _FakeTextChannel(2, "daily")
    guild = _FakeGuild([default_channel, target_channel])
    ctx = _FakeContext(guild, default_channel)

    await cog.dailysheets_set.callback(  # type: ignore[union-attr]
        cog,
        ctx,
        "#daily",
        "20:30",
        "Please",
        "update",
        "sheets",
    )

    assert ctx.sent == ["Daily sheets reminder set in #daily at `20:30 UTC`."]
    assert repo.upserts == [
        {
            "guild_id": 1,
            "channel_id": 2,
            "remind_hour_utc": 20,
            "remind_minute_utc": 30,
            "message": "Please update sheets",
        }
    ]
