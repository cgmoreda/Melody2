from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

import pytest

from cogs.voice_logging import VoiceLoggingCog


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


class _FakeBot:
    def __init__(self, guilds: list[_FakeGuild]) -> None:
        self.guilds = guilds

    def get_guild(self, guild_id: int) -> _FakeGuild | None:
        for guild in self.guilds:
            if guild.id == guild_id:
                return guild
        return None


class _FakeRepo:
    def __init__(self, open_solo_by_guild: dict[int, set[int]] | None = None) -> None:
        self._open_solo_by_guild = open_solo_by_guild or {}
        self.closed_calls: list[tuple[int, int, datetime]] = []
        self.started_calls: list[dict[str, Any]] = []

    async def get_open_solo_voice_member_ids(self, guild_id: int) -> set[int]:
        return set(self._open_solo_by_guild.get(guild_id, set()))

    async def close_open_voice_sessions(self, guild_id: int, discord_id: int, ended_at: datetime) -> int:
        self.closed_calls.append((guild_id, discord_id, ended_at))
        return 1

    async def start_voice_session(
        self,
        guild_id: int,
        discord_id: int,
        channel_id: int,
        channel_name: str,
        is_solo: bool,
        started_at: datetime,
    ) -> None:
        self.started_calls.append(
            {
                "guild_id": guild_id,
                "discord_id": discord_id,
                "channel_id": channel_id,
                "channel_name": channel_name,
                "is_solo": is_solo,
                "started_at": started_at,
            }
        )


class _FakeConfigService:
    async def get(self, guild_id: int) -> Any:
        class _Config:
            voice_check_interval_seconds = 3600
            voice_confirm_timeout_seconds = 60

        return _Config()


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

    repo = _FakeRepo(open_solo_by_guild={100: {2, 3}})
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
    assert start_call["is_solo"] is True
    assert start_call["started_at"].tzinfo is UTC

    assert sorted(started_watchdogs) == [1, 2]
