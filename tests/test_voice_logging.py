from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

import pytest

import cogs.voice_logging as voice_logging_module
from cogs.voice_logging import VoiceLoggingCog
from services.discord_output import DISCORD_MESSAGE_CHAR_LIMIT


class _FakeMember:
    def __init__(self, member_id: int, *, is_bot: bool = False) -> None:
        self.id = member_id
        self.bot = is_bot
        self.guild: _FakeGuild | None = None


class _FakeVoiceChannel:
    def __init__(self, channel_id: int, name: str, members: list[_FakeMember]) -> None:
        self.id = channel_id
        self.name = name
        self.members = members


class _FakeGuild:
    def __init__(self, guild_id: int, channels: list[_FakeVoiceChannel]) -> None:
        self.id = guild_id
        self.voice_channels = channels
        self._members: dict[int, _FakeMember] = {}
        for channel in channels:
            for member in channel.members:
                member.guild = self
                self._members[member.id] = member

    def get_member(self, member_id: int) -> _FakeMember | None:
        return self._members.get(member_id)


class _FakeVoiceState:
    def __init__(self, channel: _FakeVoiceChannel | None) -> None:
        self.channel = channel


class _FakeBot:
    def __init__(self, guilds: list[_FakeGuild]) -> None:
        self.guilds = guilds

    def get_guild(self, guild_id: int) -> _FakeGuild | None:
        for guild in self.guilds:
            if guild.id == guild_id:
                return guild
        return None


class _FakeRepo:
    def __init__(self, open_tracked_by_guild: dict[int, set[int]] | None = None) -> None:
        self._open_tracked_by_guild = open_tracked_by_guild or {}
        self.closed_calls: list[tuple[int, int, datetime]] = []
        self.started_calls: list[dict[str, Any]] = []

    async def get_open_tracked_voice_member_ids(self, guild_id: int) -> set[int]:
        return set(self._open_tracked_by_guild.get(guild_id, set()))

    async def close_open_voice_sessions(self, guild_id: int, discord_id: int, ended_at: datetime) -> int:
        self.closed_calls.append((guild_id, discord_id, ended_at))
        return 1

    async def start_voice_session(
        self,
        guild_id: int,
        discord_id: int,
        channel_id: int,
        channel_name: str,
        is_tracked: bool,
        started_at: datetime,
    ) -> None:
        self.started_calls.append(
            {
                "guild_id": guild_id,
                "discord_id": discord_id,
                "channel_id": channel_id,
                "channel_name": channel_name,
                "is_tracked": is_tracked,
                "started_at": started_at,
            }
        )


class _FakeConfigService:
    async def get(self, guild_id: int) -> Any:
        class _Config:
            voice_check_interval_seconds = 3600
            voice_confirm_timeout_seconds = 60

        return _Config()

    async def get_text(self, guild_id: int, key: str) -> str:
        return ""


@pytest.mark.asyncio
async def test_watchdog_cleanup_is_identity_safe() -> None:
    cog = VoiceLoggingCog(
        bot=object(),  # type: ignore[arg-type]
        repo=object(),  # type: ignore[arg-type]
        config_service=object(),  # type: ignore[arg-type]
    )

    async def _sleeper() -> None:
        await asyncio.sleep(30)

    old_task: asyncio.Task[None] = asyncio.create_task(_sleeper())
    new_task: asyncio.Task[None] = asyncio.create_task(_sleeper())

    try:
        cog._watchdogs[10] = new_task

        cog._clear_watchdog_if_current(10, old_task)
        assert cog._watchdogs.get(10) is new_task

        cog._clear_watchdog_if_current(10, new_task)
        assert 10 not in cog._watchdogs
    finally:
        old_task.cancel()
        new_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await old_task
        with contextlib.suppress(asyncio.CancelledError):
            await new_task


@pytest.mark.asyncio
async def test_on_ready_reconciles_stale_open_sessions_without_duplicate_starts() -> None:
    member_active_missing = _FakeMember(1)
    member_active_open = _FakeMember(2)
    member_bot = _FakeMember(99, is_bot=True)
    member_not_solo = _FakeMember(7)

    solo_channel = _FakeVoiceChannel(
        501,
        "Solo Room A",
        [member_active_missing, member_active_open, member_bot],
    )
    non_solo_channel = _FakeVoiceChannel(777, "General Voice", [member_not_solo])
    guild = _FakeGuild(100, [solo_channel, non_solo_channel])

    repo = _FakeRepo(open_tracked_by_guild={100: {2, 3}})
    cog = VoiceLoggingCog(
        bot=_FakeBot([guild]),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=_FakeConfigService(),  # type: ignore[arg-type]
    )

    started_watchdogs: list[int] = []
    cog._start_watchdog = lambda member: started_watchdogs.append(member.id)  # type: ignore[assignment]

    await cog.on_ready()

    assert len(repo.closed_calls) == 1
    closed_guild_id, closed_member_id, closed_at = repo.closed_calls[0]
    assert closed_guild_id == 100
    assert closed_member_id == 3
    assert closed_at.tzinfo is UTC

    assert len(repo.started_calls) == 1
    start_call = repo.started_calls[0]
    assert start_call["guild_id"] == 100
    assert start_call["discord_id"] == 1
    assert start_call["channel_id"] == 501
    assert start_call["channel_name"] == "Solo Room A"
    assert start_call["is_tracked"] is True
    assert start_call["started_at"].tzinfo is UTC

    assert sorted(started_watchdogs) == [1, 2]


def test_render_ranked_message_is_capped_to_discord_limit() -> None:
    lines = [f"#{index:03d} {'user' * 12} 1234.56h" for index in range(1, 300)]
    rendered = VoiceLoggingCog._render_ranked_message(
        title="**Solo Voice Hours (all time)**",
        lines=lines,
        max_lines=250,
        overflow_label="users",
    )

    assert len(rendered) <= DISCORD_MESSAGE_CHAR_LIMIT
    assert rendered.count("```") % 2 == 0


@pytest.mark.asyncio
async def test_voice_state_switch_starts_new_session_and_watchdog_for_solo_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(voice_logging_module.discord, "VoiceChannel", _FakeVoiceChannel)

    member = _FakeMember(42)
    source_channel = _FakeVoiceChannel(1001, "General VC", [member])
    target_channel = _FakeVoiceChannel(1002, "Solo Room B", [])
    guild = _FakeGuild(900, [source_channel, target_channel])
    member.guild = guild

    repo = _FakeRepo()
    cog = VoiceLoggingCog(
        bot=_FakeBot([guild]),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=_FakeConfigService(),  # type: ignore[arg-type]
    )

    stopped: list[int] = []
    started: list[int] = []
    cog._stop_watchdog = lambda member_id: stopped.append(member_id)  # type: ignore[assignment]
    cog._start_watchdog = lambda member_obj: started.append(member_obj.id)  # type: ignore[assignment]

    await cog.on_voice_state_update(
        member,  # type: ignore[arg-type]
        _FakeVoiceState(source_channel),  # type: ignore[arg-type]
        _FakeVoiceState(target_channel),  # type: ignore[arg-type]
    )

    assert stopped == [member.id]
    assert len(repo.closed_calls) == 1
    assert repo.closed_calls[0][0] == guild.id
    assert repo.closed_calls[0][1] == member.id

    assert len(repo.started_calls) == 1
    start_call = repo.started_calls[0]
    assert start_call["guild_id"] == guild.id
    assert start_call["discord_id"] == member.id
    assert start_call["channel_id"] == target_channel.id
    assert start_call["is_tracked"] is True

    assert started == [member.id]
