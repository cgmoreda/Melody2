from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

import cogs.voice_logging as voice_logging_module
from cogs.voice_logging import VoiceLoggingCog, WorkConfirmationResult
from discord.ext import commands
from db.repository import CoachConfig
from services.discord_output import DISCORD_MESSAGE_CHAR_LIMIT
from services.dynamic_voice import DynamicVoiceManager
from services.voice_service import VoiceService


# ---------------------------------------------------------------------------
# Additional fake helpers for voicehours roles/teams/unis tests
# ---------------------------------------------------------------------------


class _FakeRole:
    def __init__(self, name: str, members: list[_FakeMemberWithBot]) -> None:
        self.name = name
        self.members = members
        for member in members:
            member.roles.append(self)


class _FakeMemberWithBot:
    def __init__(
        self,
        member_id: int,
        *,
        bot: bool = False,
        display_name: str | None = None,
    ) -> None:
        self.id = member_id
        self.bot = bot
        self.display_name = display_name or f"user-{member_id}"
        self.mention = f"<@{member_id}>"
        self.roles: list[_FakeRole] = []


class _FakeGuildWithRoles:
    def __init__(self, guild_id: int, roles: list[_FakeRole]) -> None:
        self.id = guild_id
        self.roles = roles
        self.voice_channels: list[Any] = []
        self._members: dict[int, _FakeMemberWithBot] = {}
        for role in roles:
            for member in role.members:
                self._members[member.id] = member

    def get_member(self, member_id: int) -> _FakeMemberWithBot | None:
        return self._members.get(member_id)


class _FakeRepoWithTotals:
    def __init__(
        self,
        totals: dict[int, float] | None = None,
        intervals: list[dict[str, Any]] | None = None,
    ) -> None:
        self._totals = totals or {}
        self._intervals = intervals or []
        self._open_tracked_by_guild: dict[int, set[int]] = {}
        self.interval_calls: list[tuple[int, datetime, datetime]] = []
        self.get_all_calls = 0

    async def get_all(self, guild_id: int) -> list[Any]:
        self.get_all_calls += 1
        return []

    async def get_tracked_voice_totals(
        self, guild_id: int, *, now: Any, since: Any
    ) -> dict[int, float]:
        return dict(self._totals)

    async def get_open_tracked_voice_member_ids(self, guild_id: int) -> set[int]:
        return set()

    async def get_tracked_voice_summary(
        self,
        guild_id: int,
        *,
        now: datetime,
        week_since: datetime,
        month_since: datetime,
    ) -> dict[int, dict[str, float]]:
        return {
            discord_id: {"week": seconds, "month": seconds, "all_time": seconds}
            for discord_id, seconds in self._totals.items()
        }

    async def get_tracked_voice_intervals(
        self,
        guild_id: int,
        *,
        since: datetime,
        now: datetime,
    ) -> list[dict[str, Any]]:
        self.interval_calls.append((guild_id, since, now))
        return list(self._intervals)


class _FakeConfigServiceWithMaxLines:
    async def get(self, guild_id: int) -> Any:
        class _Config:
            voice_check_interval_seconds = 3600
            voice_confirm_timeout_seconds = 60
            voicehours_max_lines = 50

        return _Config()

    async def get_text(self, guild_id: int, key: str) -> str:
        return ""


class _MutableTrackedKeywordsConfig(_FakeConfigServiceWithMaxLines):
    def __init__(self, raw_keywords: str) -> None:
        self.raw_keywords = raw_keywords
        self.set_calls: list[tuple[int, str, str]] = []
        self.reset_calls: list[tuple[int, str]] = []

    async def get_text(self, guild_id: int, key: str) -> str:
        if key == "voice_tracked_keywords":
            return self.raw_keywords
        return await super().get_text(guild_id, key)

    async def set_text(self, guild_id: int, key: str, value: str) -> str:
        self.set_calls.append((guild_id, key, value))
        if key == "voice_tracked_keywords":
            self.raw_keywords = value
        return value

    async def reset_text(self, guild_id: int, key: str) -> str:
        self.reset_calls.append((guild_id, key))
        if key == "voice_tracked_keywords":
            self.raw_keywords = ""
        return ""


class _FakeContext:
    def __init__(self, guild: Any, author_id: int = 1, *, administrator: bool = False) -> None:
        self.guild = guild
        self.sent_messages: list[str] = []
        admin_value = administrator

        class _Permissions:
            administrator = admin_value

        class _Author:
            id = author_id
            display_name = "TestUser"
            guild_permissions = _Permissions()

        self.author = _Author()

    async def send(self, message: str) -> None:
        self.sent_messages.append(message)


class _FakeMember:
    def __init__(
        self,
        member_id: int,
        *,
        is_bot: bool = False,
        name: str | None = None,
        display_name: str | None = None,
    ) -> None:
        self.id = member_id
        self.bot = is_bot
        self.name = name or f"user-{member_id}"
        self.display_name = display_name or self.name
        self.mention = f"<@{member_id}>"
        self.global_name = None
        self.guild: _FakeGuild | None = None
        self.voice: _FakeVoiceState | None = None
        self.sent_messages: list[str] = []
        self.move_calls: list[tuple[Any, str | None]] = []

    async def send(self, message: str, **kwargs: Any) -> Any:
        self.sent_messages.append(message)
        return None

    async def move_to(self, channel: Any, *, reason: str | None = None) -> None:
        self.move_calls.append((channel, reason))
        self.voice = _FakeVoiceState(channel) if channel is not None else None


class _FakeVoiceChannel:
    def __init__(self, channel_id: int, name: str, members: list[_FakeMember]) -> None:
        self.id = channel_id
        self.name = name
        self.members = members


class _FakeGuild:
    def __init__(
        self,
        guild_id: int,
        channels: list[_FakeVoiceChannel],
        extra_members: list[_FakeMember] | None = None,
        afk_channel: _FakeVoiceChannel | None = None,
    ) -> None:
        self.id = guild_id
        self.name = f"guild-{guild_id}"
        self.voice_channels = channels
        self.afk_channel = afk_channel
        self.members: list[_FakeMember] = []
        self._members: dict[int, _FakeMember] = {}
        for channel in channels:
            for member in channel.members:
                member.voice = _FakeVoiceState(channel)
                self._add_member(member)
        for member in extra_members or []:
            self._add_member(member)

    def _add_member(self, member: _FakeMember) -> None:
        member.guild = self
        self._members[member.id] = member
        if member not in self.members:
            self.members.append(member)

    def get_member(self, member_id: int) -> _FakeMember | None:
        return self._members.get(member_id)

    def get_channel(self, channel_id: int) -> _FakeVoiceChannel | None:
        for channel in self.voice_channels:
            if channel.id == channel_id:
                return channel
        if self.afk_channel and self.afk_channel.id == channel_id:
            return self.afk_channel
        return None


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
        self.replaced_calls: list[dict[str, Any]] = []

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

    async def replace_voice_session(
        self,
        guild_id: int,
        discord_id: int,
        ended_at: datetime,
        *,
        channel_id: int | None = None,
        channel_name: str | None = None,
        started_at: datetime | None = None,
    ) -> int:
        self.replaced_calls.append(
            {
                "guild_id": guild_id,
                "discord_id": discord_id,
                "ended_at": ended_at,
                "channel_id": channel_id,
                "channel_name": channel_name,
                "started_at": started_at,
            }
        )
        return 1


class _FakeConfigService:
    async def get(self, guild_id: int) -> Any:
        class _Config:
            voice_check_interval_seconds = 3600
            voice_confirm_timeout_seconds = 60

        return _Config()

    async def get_text(self, guild_id: int, key: str) -> str:
        return ""


class _FakeTrainingConfigService(_FakeConfigServiceWithMaxLines):
    def __init__(self, *, max_lines: int = 50) -> None:
        self._max_lines = max_lines

    async def get(self, guild_id: int) -> Any:
        max_lines = self._max_lines

        class _Config:
            voice_check_interval_seconds = 3600
            voice_confirm_timeout_seconds = 60
            voicehours_max_lines = max_lines

        return _Config()

    async def get_text(self, guild_id: int, key: str) -> str:
        if key == "training_role_substring":
            return "Training"
        return ""


class _FakeTahzeeqConfigService(_FakeConfigServiceWithMaxLines):
    async def get_text(self, guild_id: int, key: str) -> str:
        if key == "training_role_substring":
            return "Training"
        return ""


class _FakeFastConfigService(_FakeConfigService):
    async def get(self, guild_id: int) -> Any:
        class _Config:
            voice_check_interval_seconds = 0
            voice_confirm_timeout_seconds = 60

        return _Config()


class _FakeCoachSecretary:
    def __init__(self, coach_id: int | None) -> None:
        self._coach_id = coach_id

    async def get_config(self, guild_id: int) -> CoachConfig | None:
        if self._coach_id is None:
            return None
        return CoachConfig(
            guild_id=guild_id,
            coach_id=self._coach_id,
            waiting_room_id=1,
            coach_channel_id=2,
        )


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
async def test_watchdog_dm_failure_notifies_configured_coach_and_closes_session() -> None:
    member = _FakeMember(10, display_name="Worker")
    coach = _FakeMember(20, display_name="Coach")
    solo_channel = _FakeVoiceChannel(501, "Solo Room A", [member])
    afk_channel = _FakeVoiceChannel(599, "AFK", [])
    guild = _FakeGuild(100, [solo_channel, afk_channel], extra_members=[coach], afk_channel=afk_channel)
    repo = _FakeRepo()
    cog = VoiceLoggingCog(
        bot=_FakeBot([guild]),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=_FakeFastConfigService(),  # type: ignore[arg-type]
        coach_secretary=_FakeCoachSecretary(coach.id),  # type: ignore[arg-type]
    )

    async def _dm_failed(*args: Any, **kwargs: Any) -> WorkConfirmationResult:
        return WorkConfirmationResult.DM_FAILED

    cog._ask_still_working = _dm_failed  # type: ignore[method-assign]

    await cog._watchdog_loop(guild.id, member.id)

    assert len(coach.sent_messages) == 2
    assert "Worker" in coach.sent_messages[0]
    assert "AFK room" in coach.sent_messages[1]
    assert member.move_calls == [(afk_channel, "Failed or missed solo-channel work check")]
    assert len(repo.closed_calls) == 1
    assert repo.closed_calls[0][0] == guild.id
    assert repo.closed_calls[0][1] == member.id


@pytest.mark.asyncio
async def test_watchdog_dm_failure_notifies_reda_fallback_when_no_coach_config() -> None:
    member = _FakeMember(11, display_name="SoloUser")
    fallback = _FakeMember(30, name="__reda", display_name="__reda")
    solo_channel = _FakeVoiceChannel(502, "Solo Room B", [member])
    afk_channel = _FakeVoiceChannel(598, "AFK", [])
    guild = _FakeGuild(101, [solo_channel, afk_channel], extra_members=[fallback], afk_channel=afk_channel)
    repo = _FakeRepo()
    cog = VoiceLoggingCog(
        bot=_FakeBot([guild]),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=_FakeFastConfigService(),  # type: ignore[arg-type]
        coach_secretary=_FakeCoachSecretary(None),  # type: ignore[arg-type]
    )

    async def _dm_failed(*args: Any, **kwargs: Any) -> WorkConfirmationResult:
        return WorkConfirmationResult.DM_FAILED

    cog._ask_still_working = _dm_failed  # type: ignore[method-assign]

    await cog._watchdog_loop(guild.id, member.id)

    assert len(fallback.sent_messages) == 1
    assert "SoloUser" in fallback.sent_messages[0]
    assert member.move_calls == [(afk_channel, "Failed or missed solo-channel work check")]
    assert len(repo.closed_calls) == 1
    assert repo.closed_calls[0][0] == guild.id
    assert repo.closed_calls[0][1] == member.id


@pytest.mark.asyncio
async def test_watchdog_disconnects_when_no_afk_channel_is_configured() -> None:
    member = _FakeMember(12, display_name="SoloUser")
    solo_channel = _FakeVoiceChannel(503, "Solo Room C", [member])
    guild = _FakeGuild(102, [solo_channel])
    repo = _FakeRepo()
    cog = VoiceLoggingCog(
        bot=_FakeBot([guild]),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=_FakeFastConfigService(),  # type: ignore[arg-type]
    )

    async def _timed_out(*args: Any, **kwargs: Any) -> WorkConfirmationResult:
        return WorkConfirmationResult.TIMED_OUT

    cog._ask_still_working = _timed_out  # type: ignore[method-assign]

    await cog._watchdog_loop(guild.id, member.id)

    assert member.move_calls == [(None, "Failed or missed solo-channel work check")]
    assert member.sent_messages == ["You were disconnected because you did not confirm in time."]
    assert len(repo.closed_calls) == 1


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


@pytest.mark.asyncio
async def test_on_ready_tracks_dynamic_channel_via_name_before_rebuild() -> None:
    """Dynamic channels are tracked by name even before rebuild_state() populates the manager."""
    member_in_dynamic = _FakeMember(5)

    # Channel whose name matches the dynamic pattern but is NOT yet in manager state
    dynamic_channel = _FakeVoiceChannel(601, "Test Duo #1", [member_in_dynamic])
    guild = _FakeGuild(200, [dynamic_channel])

    repo = _FakeRepo()

    # Use a real DynamicVoiceManager with empty state (simulates pre-rebuild condition)
    empty_manager = DynamicVoiceManager()
    assert not empty_manager.is_tracked(dynamic_channel.id)  # confirm state is empty

    cog = VoiceLoggingCog(
        bot=_FakeBot([guild]),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=_FakeConfigService(),  # type: ignore[arg-type]
        dynamic_voice=empty_manager,
    )

    await cog.on_ready()

    # Session must be started as tracked because the name pattern matches
    assert len(repo.started_calls) == 1
    assert repo.started_calls[0]["discord_id"] == 5
    assert repo.started_calls[0]["is_tracked"] is True


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
async def test_voice_state_switch_schedules_delayed_session_and_watchdog_for_solo_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(voice_logging_module.discord, "VoiceChannel", _FakeVoiceChannel)
    monkeypatch.setattr(voice_logging_module, "VOICE_SESSION_MIN_SECONDS", 0.0)

    member = _FakeMember(42)
    source_channel = _FakeVoiceChannel(1001, "General VC", [member])
    target_channel = _FakeVoiceChannel(1002, "Solo Room B", [])
    guild = _FakeGuild(900, [source_channel, target_channel])
    member.guild = guild
    member.voice = _FakeVoiceState(target_channel)

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
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert stopped == [member.id]
    assert repo.closed_calls == []
    assert repo.started_calls == []

    assert len(repo.replaced_calls) == 1
    replace_call = repo.replaced_calls[0]
    assert replace_call["guild_id"] == guild.id
    assert replace_call["discord_id"] == member.id
    assert replace_call["channel_id"] == target_channel.id
    assert replace_call["channel_name"] == target_channel.name
    assert replace_call["started_at"] == replace_call["ended_at"]

    assert started == [member.id]


@pytest.mark.asyncio
async def test_voice_state_leave_before_delay_writes_no_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(voice_logging_module.discord, "VoiceChannel", _FakeVoiceChannel)

    member = _FakeMember(43)
    target_channel = _FakeVoiceChannel(1003, "Solo Room C", [member])
    guild = _FakeGuild(901, [target_channel])
    member.guild = guild
    member.voice = _FakeVoiceState(target_channel)

    repo = _FakeRepo()
    cog = VoiceLoggingCog(
        bot=_FakeBot([guild]),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=_FakeConfigService(),  # type: ignore[arg-type]
    )
    cog._stop_watchdog = lambda member_id: None  # type: ignore[assignment]
    cog._start_watchdog = lambda member_obj: None  # type: ignore[assignment]

    await cog.on_voice_state_update(
        member,  # type: ignore[arg-type]
        _FakeVoiceState(None),  # type: ignore[arg-type]
        _FakeVoiceState(target_channel),  # type: ignore[arg-type]
    )
    assert len(cog._pending_voice_starts) == 1

    member.voice = None
    await cog.on_voice_state_update(
        member,  # type: ignore[arg-type]
        _FakeVoiceState(target_channel),  # type: ignore[arg-type]
        _FakeVoiceState(None),  # type: ignore[arg-type]
    )
    await asyncio.sleep(0)

    assert cog._pending_voice_starts == {}
    assert repo.replaced_calls == []
    assert repo.started_calls == []
    assert repo.closed_calls == []


@pytest.mark.asyncio
async def test_voice_state_closes_persisted_session_after_channel_is_untracked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(voice_logging_module.discord, "VoiceChannel", _FakeVoiceChannel)

    member = _FakeMember(44)
    office = _FakeVoiceChannel(1004, "Coach Office", [member])
    coffee = _FakeVoiceChannel(1005, "Coffee Room", [])
    guild = _FakeGuild(902, [office, coffee])
    member.guild = guild
    member.voice = _FakeVoiceState(coffee)

    repo = _FakeRepo()
    cog = VoiceLoggingCog(
        bot=_FakeBot([guild]),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=_MutableTrackedKeywordsConfig(""),  # type: ignore[arg-type]
    )
    cog._persisted_voice_sessions.add((guild.id, member.id))
    cog._stop_watchdog = lambda member_id: None  # type: ignore[assignment]

    await cog.on_voice_state_update(
        member,  # type: ignore[arg-type]
        _FakeVoiceState(office),  # type: ignore[arg-type]
        _FakeVoiceState(coffee),  # type: ignore[arg-type]
    )

    assert len(repo.closed_calls) == 1
    assert repo.closed_calls[0][0] == guild.id
    assert repo.closed_calls[0][1] == member.id
    assert cog._persisted_voice_sessions == set()
    assert repo.replaced_calls == []


@pytest.mark.asyncio
async def test_track_remove_closes_sessions_in_channels_that_become_untracked() -> None:
    member = _FakeMember(45)
    office = _FakeVoiceChannel(1006, "Coach Office", [member])
    guild = _FakeGuild(903, [office])

    repo = _FakeRepo()
    config = _MutableTrackedKeywordsConfig("office")
    cog = VoiceLoggingCog(
        bot=_FakeBot([guild]),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=config,  # type: ignore[arg-type]
    )
    cog._persisted_voice_sessions.add((guild.id, member.id))
    ctx = _FakeContext(guild, administrator=True)

    await cog._handle_track(ctx, ("remove", "office"))  # type: ignore[arg-type]

    assert config.raw_keywords == ""
    assert config.reset_calls == [(guild.id, "voice_tracked_keywords")]
    assert len(repo.closed_calls) == 1
    assert repo.closed_calls[0][0] == guild.id
    assert repo.closed_calls[0][1] == member.id
    assert cog._persisted_voice_sessions == set()
    assert ctx.sent_messages == [
        "Removed `office` from tracked keywords. Closed **1** now-untracked voice session(s)."
    ]


# ---------------------------------------------------------------------------
# Tests for voicehours roles/teams/unis leaderboard filtering
# ---------------------------------------------------------------------------


def _make_cog_for_leaderboard(
    guild: Any,
    totals: dict[int, float],
) -> VoiceLoggingCog:
    repo = _FakeRepoWithTotals(totals=totals)
    config_service = _FakeConfigServiceWithMaxLines()
    return VoiceLoggingCog(
        bot=object(),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=config_service,  # type: ignore[arg-type]
    )


def _freeze_voice_logging_now(monkeypatch: pytest.MonkeyPatch, fixed_now: datetime) -> None:
    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    monkeypatch.setattr(voice_logging_module, "datetime", _FixedDateTime)


@pytest.mark.asyncio
async def test_voicehours_tahzeeq_excludes_guest_role_members() -> None:
    regular_member = _FakeMemberWithBot(1, display_name="Regular User")
    guest_member = _FakeMemberWithBot(2, display_name="Guest User")
    training_role = _FakeRole("Training Arc", [regular_member, guest_member])
    _FakeRole("Guest", [guest_member])
    guild = _FakeGuildWithRoles(20, [training_role])
    repo = _FakeRepoWithTotals(totals={})
    cog = VoiceLoggingCog(
        bot=object(),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=_FakeTahzeeqConfigService(),  # type: ignore[arg-type]
    )

    ctx = _FakeContext(guild, administrator=True)
    await cog.voicehours.callback(cog, ctx, "tahzeeq", "2", "last", "1", "days")  # type: ignore[union-attr]

    output = "\n".join(ctx.sent_messages)
    assert "<@1>" in output
    assert "<@2>" not in output
    assert "Guest User" not in output


@pytest.mark.asyncio
async def test_timesheet_last_filters_training_members_and_includes_zero_hour_trainees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cairo = ZoneInfo("Africa/Cairo")
    fixed_now = datetime(2026, 1, 10, 8, 0, tzinfo=cairo).astimezone(UTC)
    _freeze_voice_logging_now(monkeypatch, fixed_now)

    regular_member = _FakeMemberWithBot(1, display_name="Regular User")
    zero_member = _FakeMemberWithBot(2, display_name="Zero User")
    guest_member = _FakeMemberWithBot(3, display_name="Guest User")
    bot_member = _FakeMemberWithBot(4, display_name="Bot User", bot=True)
    training_role = _FakeRole("Training Arc", [regular_member, zero_member, guest_member, bot_member])
    _FakeRole("Guest", [guest_member])
    guild = _FakeGuildWithRoles(30, [training_role])
    intervals = [
        {
            "discord_id": regular_member.id,
            "start_ts": datetime(2026, 1, 10, 5, 0, tzinfo=cairo).astimezone(UTC),
            "end_ts": datetime(2026, 1, 10, 7, 0, tzinfo=cairo).astimezone(UTC),
        },
        {
            "discord_id": guest_member.id,
            "start_ts": datetime(2026, 1, 10, 5, 0, tzinfo=cairo).astimezone(UTC),
            "end_ts": datetime(2026, 1, 10, 8, 0, tzinfo=cairo).astimezone(UTC),
        },
        {
            "discord_id": bot_member.id,
            "start_ts": datetime(2026, 1, 10, 5, 0, tzinfo=cairo).astimezone(UTC),
            "end_ts": datetime(2026, 1, 10, 8, 0, tzinfo=cairo).astimezone(UTC),
        },
    ]
    repo = _FakeRepoWithTotals(intervals=intervals)
    cog = VoiceLoggingCog(
        bot=object(),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=_FakeTrainingConfigService(),  # type: ignore[arg-type]
    )

    ctx = _FakeContext(guild)
    await cog.timesheet.callback(cog, ctx, "last", "2", "days")  # type: ignore[union-attr]

    output = "\n".join(ctx.sent_messages)
    assert "Training Voice Timesheet" in output
    assert "Regular User" in output
    assert "Zero User" in output
    assert "Guest User" not in output
    assert "Bot User" not in output
    assert repo.interval_calls[0][1] == datetime(2026, 1, 9, 5, 0, tzinfo=cairo).astimezone(UTC)
    assert repo.interval_calls[0][2] == fixed_now


@pytest.mark.asyncio
async def test_timesheet_output_respects_voicehours_max_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cairo = ZoneInfo("Africa/Cairo")
    _freeze_voice_logging_now(monkeypatch, datetime(2026, 1, 10, 8, 0, tzinfo=cairo).astimezone(UTC))

    members = [_FakeMemberWithBot(index, display_name=f"User {index}") for index in range(1, 4)]
    training_role = _FakeRole("Training Arc", members)
    guild = _FakeGuildWithRoles(31, [training_role])
    repo = _FakeRepoWithTotals()
    cog = VoiceLoggingCog(
        bot=object(),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=_FakeTrainingConfigService(max_lines=1),  # type: ignore[arg-type]
    )

    ctx = _FakeContext(guild)
    await cog.timesheet.callback(cog, ctx, "last", "1", "day")  # type: ignore[union-attr]

    output = "\n".join(ctx.sent_messages)
    assert "User 1" in output
    assert "User 2" not in output
    assert "... and 2 more users" in output


@pytest.mark.asyncio
async def test_voicehours_max_range_reports_training_members_sorted_by_best_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cairo = ZoneInfo("Africa/Cairo")
    fixed_now = datetime(2026, 1, 10, 12, 0, tzinfo=cairo).astimezone(UTC)
    _freeze_voice_logging_now(monkeypatch, fixed_now)

    regular_member = _FakeMemberWithBot(1, display_name="Regular User")
    zero_member = _FakeMemberWithBot(2, display_name="Zero User")
    training_role = _FakeRole("Training Arc", [regular_member, zero_member])
    guild = _FakeGuildWithRoles(32, [training_role])
    repo = _FakeRepoWithTotals(
        intervals=[
            {
                "discord_id": regular_member.id,
                "start_ts": datetime(2026, 1, 10, 6, 30, tzinfo=cairo).astimezone(UTC),
                "end_ts": datetime(2026, 1, 10, 8, 30, tzinfo=cairo).astimezone(UTC),
            }
        ]
    )
    cog = VoiceLoggingCog(
        bot=object(),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=_FakeTrainingConfigService(),  # type: ignore[arg-type]
    )

    ctx = _FakeContext(guild)
    await cog.voicehours.callback(  # type: ignore[union-attr]
        cog,
        ctx,
        "max",
        "range",
        "2",
        "hours",
        "last",
        "1",
        "week",
    )

    output = "\n".join(ctx.sent_messages)
    assert "Max Tracked Voice Range" in output
    assert "Regular User" in output
    assert "Zero User" in output
    assert output.index("Regular User") < output.index("Zero User")
    assert "2.00h" in output
    assert repo.interval_calls[0][1] == datetime(2026, 1, 3, 5, 0, tzinfo=cairo).astimezone(UTC)
    assert repo.interval_calls[0][2] == fixed_now


@pytest.mark.asyncio
async def test_voicehours_top_limit_max_day_renders_limited_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cairo = ZoneInfo("Africa/Cairo")
    fixed_now = datetime(2026, 1, 20, 12, 0, tzinfo=cairo).astimezone(UTC)
    _freeze_voice_logging_now(monkeypatch, fixed_now)

    members = [_FakeMemberWithBot(index, display_name=f"User {index}") for index in range(1, 5)]
    guild = _FakeGuildWithRoles(34, [_FakeRole("Training Arc", members)])
    day_start = datetime(2026, 1, 10, 5, 0, tzinfo=cairo)
    repo = _FakeRepoWithTotals(
        intervals=[
            {
                "discord_id": member.id,
                "start_ts": day_start.astimezone(UTC),
                "end_ts": (day_start + timedelta(hours=5 - member.id)).astimezone(UTC),
            }
            for member in members
        ]
    )
    cog = VoiceLoggingCog(
        bot=object(),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=_FakeTrainingConfigService(),  # type: ignore[arg-type]
    )

    ctx = _FakeContext(guild)
    await cog.voicehours.callback(  # type: ignore[union-attr]
        cog,
        ctx,
        "top",
        "3",
        "max",
        "day",
        "last",
        "3",
        "week",
    )

    output = "\n".join(ctx.sent_messages)
    assert "Max Tracked Voice Day" in output
    assert "User 1" in output
    assert "User 2" in output
    assert "User 3" in output
    assert "User 4" not in output
    assert "... and 1 more users" in output


@pytest.mark.asyncio
async def test_timesheet_top_limit_max_range_renders_limited_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cairo = ZoneInfo("Africa/Cairo")
    fixed_now = datetime(2026, 1, 10, 12, 0, tzinfo=cairo).astimezone(UTC)
    _freeze_voice_logging_now(monkeypatch, fixed_now)

    members = [_FakeMemberWithBot(index, display_name=f"User {index}") for index in range(1, 4)]
    guild = _FakeGuildWithRoles(35, [_FakeRole("Training Arc", members)])
    start = datetime(2026, 1, 10, 6, 0, tzinfo=cairo)
    repo = _FakeRepoWithTotals(
        intervals=[
            {
                "discord_id": member.id,
                "start_ts": start.astimezone(UTC),
                "end_ts": (start + timedelta(hours=4 - member.id)).astimezone(UTC),
            }
            for member in members
        ]
    )
    cog = VoiceLoggingCog(
        bot=object(),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=_FakeTrainingConfigService(),  # type: ignore[arg-type]
    )

    ctx = _FakeContext(guild)
    await cog.timesheet.callback(  # type: ignore[union-attr]
        cog,
        ctx,
        "top",
        "2",
        "max",
        "range",
        "3",
        "hours",
        "last",
        "1",
        "week",
    )

    output = "\n".join(ctx.sent_messages)
    assert "Max Tracked Voice Range" in output
    assert "User 1" in output
    assert "User 2" in output
    assert "User 3" not in output
    assert "... and 1 more users" in output


@pytest.mark.asyncio
async def test_voicehours_max_day_suffix_top_limit_renders_limited_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cairo = ZoneInfo("Africa/Cairo")
    fixed_now = datetime(2026, 1, 20, 12, 0, tzinfo=cairo).astimezone(UTC)
    _freeze_voice_logging_now(monkeypatch, fixed_now)

    members = [_FakeMemberWithBot(index, display_name=f"User {index}") for index in range(1, 5)]
    guild = _FakeGuildWithRoles(38, [_FakeRole("Training Arc", members)])
    day_start = datetime(2026, 1, 10, 5, 0, tzinfo=cairo)
    repo = _FakeRepoWithTotals(
        intervals=[
            {
                "discord_id": member.id,
                "start_ts": day_start.astimezone(UTC),
                "end_ts": (day_start + timedelta(hours=5 - member.id)).astimezone(UTC),
            }
            for member in members
        ]
    )
    cog = VoiceLoggingCog(
        bot=object(),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=_FakeTrainingConfigService(),  # type: ignore[arg-type]
    )

    ctx = _FakeContext(guild)
    await cog.voicehours.callback(  # type: ignore[union-attr]
        cog,
        ctx,
        "max",
        "day",
        "last",
        "3",
        "week",
        "top",
        "3",
    )

    output = "\n".join(ctx.sent_messages)
    assert "Max Tracked Voice Day" in output
    assert "User 1" in output
    assert "User 2" in output
    assert "User 3" in output
    assert "User 4" not in output
    assert "... and 1 more users" in output


@pytest.mark.asyncio
async def test_timesheet_max_range_suffix_top_limit_renders_limited_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cairo = ZoneInfo("Africa/Cairo")
    fixed_now = datetime(2026, 1, 10, 12, 0, tzinfo=cairo).astimezone(UTC)
    _freeze_voice_logging_now(monkeypatch, fixed_now)

    members = [_FakeMemberWithBot(index, display_name=f"User {index}") for index in range(1, 4)]
    guild = _FakeGuildWithRoles(39, [_FakeRole("Training Arc", members)])
    start = datetime(2026, 1, 10, 6, 0, tzinfo=cairo)
    repo = _FakeRepoWithTotals(
        intervals=[
            {
                "discord_id": member.id,
                "start_ts": start.astimezone(UTC),
                "end_ts": (start + timedelta(hours=4 - member.id)).astimezone(UTC),
            }
            for member in members
        ]
    )
    cog = VoiceLoggingCog(
        bot=object(),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=_FakeTrainingConfigService(),  # type: ignore[arg-type]
    )

    ctx = _FakeContext(guild)
    await cog.timesheet.callback(  # type: ignore[union-attr]
        cog,
        ctx,
        "max",
        "range",
        "3",
        "hours",
        "last",
        "1",
        "week",
        "top",
        "2",
    )

    output = "\n".join(ctx.sent_messages)
    assert "Max Tracked Voice Range" in output
    assert "User 1" in output
    assert "User 2" in output
    assert "User 3" not in output
    assert "... and 1 more users" in output


@pytest.mark.asyncio
async def test_timesheet_max_day_suffix_top_limit_renders_limited_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cairo = ZoneInfo("Africa/Cairo")
    fixed_now = datetime(2026, 1, 20, 12, 0, tzinfo=cairo).astimezone(UTC)
    _freeze_voice_logging_now(monkeypatch, fixed_now)

    members = [_FakeMemberWithBot(index, display_name=f"User {index}") for index in range(1, 5)]
    guild = _FakeGuildWithRoles(48, [_FakeRole("Training Arc", members)])
    day_start = datetime(2026, 1, 10, 5, 0, tzinfo=cairo)
    repo = _FakeRepoWithTotals(
        intervals=[
            {
                "discord_id": member.id,
                "start_ts": day_start.astimezone(UTC),
                "end_ts": (day_start + timedelta(hours=5 - member.id)).astimezone(UTC),
            }
            for member in members
        ]
    )
    cog = VoiceLoggingCog(
        bot=object(),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=_FakeTrainingConfigService(),  # type: ignore[arg-type]
    )

    ctx = _FakeContext(guild)
    await cog.timesheet.callback(  # type: ignore[union-attr]
        cog,
        ctx,
        "max",
        "day",
        "last",
        "3",
        "week",
        "top",
        "3",
    )

    output = "\n".join(ctx.sent_messages)
    assert "Max Tracked Voice Day" in output
    assert "User 1" in output
    assert "User 2" in output
    assert "User 3" in output
    assert "User 4" not in output
    assert "... and 1 more users" in output


@pytest.mark.asyncio
async def test_voicehours_top_limit_regular_leaderboard_still_uses_totals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
    _freeze_voice_logging_now(monkeypatch, fixed_now)

    members = [_FakeMemberWithBot(index, display_name=f"User {index}") for index in range(1, 5)]
    guild = _FakeGuildWithRoles(36, [_FakeRole("Training Arc", members)])
    repo = _FakeRepoWithTotals(
        totals={
            1: 4 * 3600.0,
            2: 3 * 3600.0,
            3: 2 * 3600.0,
            4: 3600.0,
        }
    )
    cog = VoiceLoggingCog(
        bot=object(),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=_FakeTrainingConfigService(),  # type: ignore[arg-type]
    )

    ctx = _FakeContext(guild)
    await cog.voicehours.callback(  # type: ignore[union-attr]
        cog,
        ctx,
        "top",
        "3",
        "last",
        "1",
        "week",
    )

    output = "\n".join(ctx.sent_messages)
    assert "Top Tracked Voice Hours" in output
    assert "Max Tracked Voice" not in output
    assert "User 1" in output
    assert "User 2" in output
    assert "User 3" in output
    assert "User 4" not in output
    assert repo.interval_calls == []


@pytest.mark.asyncio
async def test_voicehours_window_before_top_limit_uses_regular_leaderboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
    _freeze_voice_logging_now(monkeypatch, fixed_now)

    members = [_FakeMemberWithBot(index, display_name=f"User {index}") for index in range(1, 5)]
    guild = _FakeGuildWithRoles(42, [_FakeRole("Training Arc", members)])
    repo = _FakeRepoWithTotals(
        totals={
            1: 4 * 3600.0,
            2: 3 * 3600.0,
            3: 2 * 3600.0,
            4: 3600.0,
        }
    )
    cog = VoiceLoggingCog(
        bot=object(),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=_FakeTrainingConfigService(),  # type: ignore[arg-type]
    )

    ctx = _FakeContext(guild)
    await cog.voicehours.callback(  # type: ignore[union-attr]
        cog,
        ctx,
        "last",
        "1",
        "week",
        "top",
        "3",
    )

    output = "\n".join(ctx.sent_messages)
    assert "Top Tracked Voice Hours" in output
    assert "User 1" in output
    assert "User 2" in output
    assert "User 3" in output
    assert "User 4" not in output
    assert "... and 1 more users" in output


@pytest.mark.asyncio
async def test_voicehours_window_before_user_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
    _freeze_voice_logging_now(monkeypatch, fixed_now)

    target_member = _FakeMemberWithBot(2, display_name="Target User")
    guild = _FakeGuildWithRoles(43, [_FakeRole("Training Arc", [target_member])])
    repo = _FakeRepoWithTotals(totals={2: 3600.0})
    cog = VoiceLoggingCog(
        bot=object(),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=_FakeTrainingConfigService(),  # type: ignore[arg-type]
    )

    async def _fake_convert(self: object, ctx: object, argument: str) -> _FakeMemberWithBot:
        assert argument == "<@2>"
        return target_member

    monkeypatch.setattr(voice_logging_module.commands.MemberConverter, "convert", _fake_convert)

    ctx = _FakeContext(guild)
    await cog.voicehours.callback(  # type: ignore[union-attr]
        cog,
        ctx,
        "last",
        "1",
        "week",
        "user",
        "<@2>",
    )

    assert ctx.sent_messages == ["**Target User** in last 1 week: **1.00h** (rank **#1**)"]
    assert repo.get_all_calls == 0


@pytest.mark.asyncio
async def test_voicehours_window_before_role_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
    _freeze_voice_logging_now(monkeypatch, fixed_now)

    members = [_FakeMemberWithBot(1, display_name="Role User"), _FakeMemberWithBot(2, display_name="Other User")]
    target_role = _FakeRole("Training Arc", members)
    guild = _FakeGuildWithRoles(44, [target_role])
    repo = _FakeRepoWithTotals(totals={1: 3600.0, 2: 1800.0})
    cog = VoiceLoggingCog(
        bot=object(),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=_FakeTrainingConfigService(),  # type: ignore[arg-type]
    )

    async def _fake_convert(self: object, ctx: object, argument: str) -> _FakeRole:
        assert argument == "<@&10>"
        return target_role

    monkeypatch.setattr(voice_logging_module.commands.RoleConverter, "convert", _fake_convert)

    ctx = _FakeContext(guild)
    await cog.voicehours.callback(  # type: ignore[union-attr]
        cog,
        ctx,
        "last",
        "1",
        "week",
        "role",
        "<@&10>",
    )

    output = "\n".join(ctx.sent_messages)
    assert "Tracked Voice Hours for Training Arc (last 1 week)" in output
    assert "Role User" in output
    assert "Other User" in output


@pytest.mark.asyncio
async def test_voicehours_window_before_tahzeeq_mode() -> None:
    regular_member = _FakeMemberWithBot(1, display_name="Regular User")
    guild = _FakeGuildWithRoles(45, [_FakeRole("Training Arc", [regular_member])])
    cog = VoiceLoggingCog(
        bot=object(),  # type: ignore[arg-type]
        repo=_FakeRepoWithTotals(totals={}),  # type: ignore[arg-type]
        config_service=_FakeTahzeeqConfigService(),  # type: ignore[arg-type]
    )

    ctx = _FakeContext(guild, administrator=True)
    await cog.voicehours.callback(  # type: ignore[union-attr]
        cog,
        ctx,
        "last",
        "1",
        "week",
        "tahzeeq",
        "2",
    )

    output = "\n".join(ctx.sent_messages)
    assert "Tahzeeq Report (last 1 week)" in output
    assert "<@1>" in output
    assert "Target: **2.00h**" in output


@pytest.mark.asyncio
async def test_voicehours_top_max_requires_numeric_limit() -> None:
    regular_member = _FakeMemberWithBot(1, display_name="Regular User")
    guild = _FakeGuildWithRoles(37, [_FakeRole("Training Arc", [regular_member])])
    cog = VoiceLoggingCog(
        bot=object(),  # type: ignore[arg-type]
        repo=_FakeRepoWithTotals(),  # type: ignore[arg-type]
        config_service=_FakeTrainingConfigService(),  # type: ignore[arg-type]
    )

    ctx = _FakeContext(guild)
    await cog.voicehours.callback(  # type: ignore[union-attr]
        cog,
        ctx,
        "top",
        "x",
        "max",
        "day",
        "last",
        "3",
        "week",
    )

    assert ctx.sent_messages == ["`limit` must be a positive integer."]


@pytest.mark.asyncio
async def test_voicehours_max_suffix_top_requires_numeric_limit() -> None:
    regular_member = _FakeMemberWithBot(1, display_name="Regular User")
    guild = _FakeGuildWithRoles(40, [_FakeRole("Training Arc", [regular_member])])
    cog = VoiceLoggingCog(
        bot=object(),  # type: ignore[arg-type]
        repo=_FakeRepoWithTotals(),  # type: ignore[arg-type]
        config_service=_FakeTrainingConfigService(),  # type: ignore[arg-type]
    )

    ctx = _FakeContext(guild)
    await cog.voicehours.callback(  # type: ignore[union-attr]
        cog,
        ctx,
        "max",
        "day",
        "last",
        "3",
        "week",
        "top",
        "x",
    )

    assert ctx.sent_messages == ["`limit` must be a positive integer."]


@pytest.mark.asyncio
async def test_voicehours_max_rejects_prefix_and_suffix_top_limits() -> None:
    regular_member = _FakeMemberWithBot(1, display_name="Regular User")
    guild = _FakeGuildWithRoles(41, [_FakeRole("Training Arc", [regular_member])])
    cog = VoiceLoggingCog(
        bot=object(),  # type: ignore[arg-type]
        repo=_FakeRepoWithTotals(),  # type: ignore[arg-type]
        config_service=_FakeTrainingConfigService(),  # type: ignore[arg-type]
    )

    ctx = _FakeContext(guild)
    await cog.voicehours.callback(  # type: ignore[union-attr]
        cog,
        ctx,
        "top",
        "5",
        "max",
        "day",
        "last",
        "3",
        "week",
        "top",
        "3",
    )

    assert ctx.sent_messages == ["Only one `top` clause is allowed."]


@pytest.mark.asyncio
async def test_voicehours_rejects_duplicate_top_clauses() -> None:
    regular_member = _FakeMemberWithBot(1, display_name="Regular User")
    guild = _FakeGuildWithRoles(46, [_FakeRole("Training Arc", [regular_member])])
    cog = VoiceLoggingCog(
        bot=object(),  # type: ignore[arg-type]
        repo=_FakeRepoWithTotals(),  # type: ignore[arg-type]
        config_service=_FakeTrainingConfigService(),  # type: ignore[arg-type]
    )

    ctx = _FakeContext(guild)
    await cog.voicehours.callback(  # type: ignore[union-attr]
        cog,
        ctx,
        "top",
        "3",
        "last",
        "1",
        "week",
        "top",
        "2",
    )

    assert ctx.sent_messages == ["Only one `top` clause is allowed."]


@pytest.mark.asyncio
async def test_voicehours_rejects_duplicate_last_windows() -> None:
    regular_member = _FakeMemberWithBot(1, display_name="Regular User")
    guild = _FakeGuildWithRoles(47, [_FakeRole("Training Arc", [regular_member])])
    cog = VoiceLoggingCog(
        bot=object(),  # type: ignore[arg-type]
        repo=_FakeRepoWithTotals(),  # type: ignore[arg-type]
        config_service=_FakeTrainingConfigService(),  # type: ignore[arg-type]
    )

    ctx = _FakeContext(guild)
    await cog.voicehours.callback(  # type: ignore[union-attr]
        cog,
        ctx,
        "last",
        "1",
        "week",
        "last",
        "2",
        "weeks",
    )

    assert ctx.sent_messages == ["Only one `last` clause is allowed."]


@pytest.mark.asyncio
async def test_timesheet_max_range_requires_explicit_lookback() -> None:
    regular_member = _FakeMemberWithBot(1, display_name="Regular User")
    guild = _FakeGuildWithRoles(33, [_FakeRole("Training Arc", [regular_member])])
    cog = VoiceLoggingCog(
        bot=object(),  # type: ignore[arg-type]
        repo=_FakeRepoWithTotals(),  # type: ignore[arg-type]
        config_service=_FakeTrainingConfigService(),  # type: ignore[arg-type]
    )

    ctx = _FakeContext(guild)
    await cog.timesheet.callback(cog, ctx, "max", "range", "2", "hours")  # type: ignore[union-attr]

    assert ctx.sent_messages == [VoiceService.MAX_USAGE]


@pytest.mark.asyncio
async def test_voicehours_roles_only_includes_team_prefixed_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only roles whose names start with 'team ' (case-insensitive) appear in roles/teams mode."""
    team_member = _FakeMemberWithBot(1)
    other_member = _FakeMemberWithBot(2)
    team_role = _FakeRole("Team Alpha", [team_member])
    uni_role = _FakeRole("UniRed", [other_member])
    guild = _FakeGuildWithRoles(10, [team_role, uni_role])

    totals = {1: 3600.0, 2: 7200.0}
    repo = _FakeRepoWithTotals(totals=totals)
    cog = VoiceLoggingCog(
        bot=object(),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=_FakeConfigServiceWithMaxLines(),  # type: ignore[arg-type]
    )

    sent: list[str] = []

    async def _fake_send_chunks(ctx: Any, content: str) -> None:
        sent.append(content)

    monkeypatch.setattr(voice_logging_module, "send_context_text_chunks", _fake_send_chunks)

    ctx = _FakeContext(guild)
    await cog.voicehours.callback(cog, ctx, "roles")  # type: ignore[union-attr]

    assert len(sent) == 1
    output = sent[0]
    assert "Team Alpha" in output
    assert "UniRed" not in output
    assert "Team Role Voice Standings" in output
    assert repo.get_all_calls == 0


@pytest.mark.asyncio
async def test_voicehours_teams_alias_behaves_like_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'teams' is an alias for 'roles' and uses the same team-prefix filter."""
    team_member = _FakeMemberWithBot(3)
    team_role = _FakeRole("Team Beta", [team_member])
    guild = _FakeGuildWithRoles(11, [team_role])

    totals = {3: 1800.0}
    repo = _FakeRepoWithTotals(totals=totals)
    cog = VoiceLoggingCog(
        bot=object(),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=_FakeConfigServiceWithMaxLines(),  # type: ignore[arg-type]
    )

    sent: list[str] = []

    async def _fake_send_chunks(ctx: Any, content: str) -> None:
        sent.append(content)

    monkeypatch.setattr(voice_logging_module, "send_context_text_chunks", _fake_send_chunks)

    ctx = _FakeContext(guild)
    await cog.voicehours.callback(cog, ctx, "teams")  # type: ignore[union-attr]

    assert len(sent) == 1
    assert "Team Role Voice Standings" in sent[0]
    assert repo.get_all_calls == 0


@pytest.mark.asyncio
async def test_voicehours_roles_empty_state_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no 'team ' roles exist the correct empty-state message is sent."""
    guild = _FakeGuildWithRoles(12, [_FakeRole("UniRed", [_FakeMemberWithBot(4)])])
    cog = _make_cog_for_leaderboard(guild, {})

    monkeypatch.setattr(
        voice_logging_module,
        "send_context_text_chunks",
        lambda ctx, content: None,
    )

    ctx = _FakeContext(guild)
    await cog.voicehours.callback(cog, ctx, "roles")  # type: ignore[union-attr]

    assert len(ctx.sent_messages) == 1
    assert "`team `" in ctx.sent_messages[0]


@pytest.mark.asyncio
async def test_voicehours_unis_only_includes_uni_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'unis' mode filters by 'uni' substring (case-insensitive) and not by team prefix."""
    uni_member = _FakeMemberWithBot(5)
    team_member = _FakeMemberWithBot(6)
    uni_role = _FakeRole("UniRed", [uni_member])
    team_role = _FakeRole("Team Alpha", [team_member])
    # Also check case-insensitivity: 'UniVersity' should match
    uni_upper_member = _FakeMemberWithBot(7)
    uni_upper_role = _FakeRole("UniVersity", [uni_upper_member])
    guild = _FakeGuildWithRoles(13, [uni_role, team_role, uni_upper_role])

    totals = {5: 1800.0, 6: 3600.0, 7: 900.0}
    repo = _FakeRepoWithTotals(totals=totals)
    cog = VoiceLoggingCog(
        bot=object(),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=_FakeConfigServiceWithMaxLines(),  # type: ignore[arg-type]
    )

    sent: list[str] = []

    async def _fake_send_chunks(ctx: Any, content: str) -> None:
        sent.append(content)

    monkeypatch.setattr(voice_logging_module, "send_context_text_chunks", _fake_send_chunks)

    ctx = _FakeContext(guild)
    await cog.voicehours.callback(cog, ctx, "unis")  # type: ignore[union-attr]

    assert len(sent) == 1
    output = sent[0]
    assert "UniRed" in output
    assert "UniVersity" in output
    assert "Team Alpha" not in output
    assert "Uni Role Voice Standings" in output
    assert repo.get_all_calls == 0


@pytest.mark.asyncio
async def test_voicehours_unis_empty_state_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no 'uni' roles exist the correct empty-state message is sent for unis mode."""
    guild = _FakeGuildWithRoles(14, [_FakeRole("Team Alpha", [_FakeMemberWithBot(8)])])
    cog = _make_cog_for_leaderboard(guild, {})

    monkeypatch.setattr(
        voice_logging_module,
        "send_context_text_chunks",
        lambda ctx, content: None,
    )

    ctx = _FakeContext(guild)
    await cog.voicehours.callback(cog, ctx, "unis")  # type: ignore[union-attr]

    assert len(ctx.sent_messages) == 1
    assert "`uni`" in ctx.sent_messages[0]


# ---------------------------------------------------------------------------
# Tests for voicehours total subcommand — parsing
# ---------------------------------------------------------------------------


def test_parse_voicehours_total_single_role() -> None:
    request = VoiceLoggingCog._parse_voicehours_request(
        ("total", "<@&111>"), default_top_limit=50,
    )
    assert request.action == "total"
    assert request.total_targets == ("<@&111>",)
    assert request.window_tokens == ()


def test_parse_voicehours_total_everyone() -> None:
    request = VoiceLoggingCog._parse_voicehours_request(
        ("total", "everyone"), default_top_limit=50,
    )
    assert request.action == "total"
    assert request.total_targets == ("everyone",)


def test_parse_voicehours_total_multiple_targets() -> None:
    request = VoiceLoggingCog._parse_voicehours_request(
        ("total", "<@&111>", "<@&222>"), default_top_limit=50,
    )
    assert request.action == "total"
    assert request.total_targets == ("<@&111>", "<@&222>")
    assert request.window_tokens == ()


def test_parse_voicehours_total_with_window() -> None:
    request = VoiceLoggingCog._parse_voicehours_request(
        ("total", "<@&111>", "last", "1", "week"), default_top_limit=50,
    )
    assert request.action == "total"
    assert request.total_targets == ("<@&111>",)
    assert request.window_tokens == ("last", "1", "week")


def test_parse_voicehours_total_with_date() -> None:
    request = VoiceLoggingCog._parse_voicehours_request(
        ("total", "<@&111>", "15/5"), default_top_limit=50,
    )
    assert request.action == "total"
    assert request.total_targets == ("<@&111>",)
    assert request.window_tokens == ("15/5",)


def test_parse_voicehours_total_role_and_user_with_window() -> None:
    request = VoiceLoggingCog._parse_voicehours_request(
        ("total", "<@&111>", "<@222>", "last", "2", "days"), default_top_limit=50,
    )
    assert request.action == "total"
    assert request.total_targets == ("<@&111>", "<@222>")
    assert request.window_tokens == ("last", "2", "days")


def test_parse_voicehours_total_no_targets_raises() -> None:
    with pytest.raises(ValueError, match="Usage"):
        VoiceLoggingCog._parse_voicehours_request(("total",), default_top_limit=50)


# ---------------------------------------------------------------------------
# Tests for voicehours total subcommand — integration (handler)
# ---------------------------------------------------------------------------


class _FakeRoleForTotal:
    def __init__(self, role_id: int, name: str, members: list[Any]) -> None:
        self.id = role_id
        self.name = name
        self.members = members


class _FakeGuildForTotal:
    def __init__(
        self,
        guild_id: int,
        members: list[_FakeMemberWithBot],
        roles: list[_FakeRoleForTotal] | None = None,
    ) -> None:
        self.id = guild_id
        self.roles = roles or []
        self.voice_channels: list[Any] = []
        self._members_by_id = {m.id: m for m in members}
        self.members = members

    def get_member(self, member_id: int) -> _FakeMemberWithBot | None:
        return self._members_by_id.get(member_id)


class _FakeContextForTotal:
    """Context with role/member converter support for total tests."""

    def __init__(
        self,
        guild: _FakeGuildForTotal,
        *,
        role_map: dict[str, _FakeRoleForTotal] | None = None,
        member_map: dict[str, _FakeMemberWithBot] | None = None,
    ) -> None:
        self.guild = guild
        self.sent_messages: list[str] = []
        self._role_map = role_map or {}
        self._member_map = member_map or {}

        class _Author:
            id = 1
            display_name = "TestUser"

            class guild_permissions:
                administrator = False

        self.author = _Author()

    async def send(self, message: str) -> None:
        self.sent_messages.append(message)


def _make_total_cog(
    guild: Any,
    totals: dict[int, float],
) -> VoiceLoggingCog:
    repo = _FakeRepoWithTotals(totals=totals)
    config_service = _FakeConfigServiceWithMaxLines()
    return VoiceLoggingCog(
        bot=object(),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=config_service,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_voicehours_total_single_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """Total for a single role sums only members in that role."""
    m1 = _FakeMemberWithBot(10, display_name="Alice")
    m2 = _FakeMemberWithBot(20, display_name="Bob")
    m3 = _FakeMemberWithBot(30, display_name="Charlie")
    role = _FakeRoleForTotal(111, "TeamA", [m1, m2])
    guild = _FakeGuildForTotal(1, [m1, m2, m3], [role])

    totals = {10: 3600.0, 20: 7200.0, 30: 1800.0}
    cog = _make_total_cog(guild, totals)

    # Patch RoleConverter to return our fake role
    async def _convert_role(self, ctx, arg):
        if arg == "<@&111>":
            return role
        raise commands.BadArgument()

    async def _convert_member(self, ctx, arg):
        raise commands.BadArgument()

    monkeypatch.setattr(commands.RoleConverter, "convert", _convert_role)
    monkeypatch.setattr(commands.MemberConverter, "convert", _convert_member)

    ctx = _FakeContextForTotal(guild)
    await cog.voicehours.callback(cog, ctx, "total", "<@&111>")  # type: ignore[union-attr]

    assert len(ctx.sent_messages) == 1
    msg = ctx.sent_messages[0]
    assert "TeamA" in msg
    assert "3.00h" in msg  # 3600 + 7200 = 10800 = 3.00h
    assert "2 members" in msg


@pytest.mark.asyncio
async def test_voicehours_total_everyone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Total for 'everyone' sums all non-bot guild members."""
    m1 = _FakeMemberWithBot(10)
    m2 = _FakeMemberWithBot(20)
    bot_member = _FakeMemberWithBot(99, bot=True)
    guild = _FakeGuildForTotal(1, [m1, m2, bot_member])

    totals = {10: 3600.0, 20: 7200.0, 99: 9999.0}
    cog = _make_total_cog(guild, totals)

    ctx = _FakeContextForTotal(guild)
    await cog.voicehours.callback(cog, ctx, "total", "everyone")  # type: ignore[union-attr]

    assert len(ctx.sent_messages) == 1
    msg = ctx.sent_messages[0]
    assert "everyone" in msg
    assert "3.00h" in msg  # 3600 + 7200 = 10800 = 3.00h (bot excluded)
    assert "2 members" in msg


@pytest.mark.asyncio
async def test_voicehours_total_two_roles_deduplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    """When a member is in both roles, their hours are counted once."""
    m1 = _FakeMemberWithBot(10, display_name="Alice")
    m2 = _FakeMemberWithBot(20, display_name="Bob")
    roleA = _FakeRoleForTotal(111, "TeamA", [m1, m2])
    roleB = _FakeRoleForTotal(222, "TeamB", [m2])  # Bob in both roles
    guild = _FakeGuildForTotal(1, [m1, m2], [roleA, roleB])

    totals = {10: 3600.0, 20: 7200.0}
    cog = _make_total_cog(guild, totals)

    async def _convert_role(self, ctx, arg):
        if arg == "<@&111>":
            return roleA
        if arg == "<@&222>":
            return roleB
        raise commands.BadArgument()

    async def _convert_member(self, ctx, arg):
        raise commands.BadArgument()

    monkeypatch.setattr(commands.RoleConverter, "convert", _convert_role)
    monkeypatch.setattr(commands.MemberConverter, "convert", _convert_member)

    ctx = _FakeContextForTotal(guild)
    await cog.voicehours.callback(cog, ctx, "total", "<@&111>", "<@&222>")  # type: ignore[union-attr]

    assert len(ctx.sent_messages) == 1
    msg = ctx.sent_messages[0]
    assert "TeamA + TeamB" in msg
    assert "3.00h" in msg  # 3600 + 7200 = 10800 = 3.00h (Bob counted once)
    assert "2 members" in msg


@pytest.mark.asyncio
async def test_voicehours_total_role_plus_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """Total for a role + user combines both, de-duplicating."""
    m1 = _FakeMemberWithBot(10, display_name="Alice")
    m2 = _FakeMemberWithBot(20, display_name="Bob")
    m3 = _FakeMemberWithBot(30, display_name="Charlie")
    role = _FakeRoleForTotal(111, "TeamA", [m1])
    guild = _FakeGuildForTotal(1, [m1, m2, m3], [role])

    totals = {10: 3600.0, 30: 1800.0}
    cog = _make_total_cog(guild, totals)

    async def _convert_role(self, ctx, arg):
        if arg == "<@&111>":
            return role
        raise commands.BadArgument()

    async def _convert_member(self, ctx, arg):
        if arg == "<@30>":
            return m3
        raise commands.BadArgument()

    monkeypatch.setattr(commands.RoleConverter, "convert", _convert_role)
    monkeypatch.setattr(commands.MemberConverter, "convert", _convert_member)

    ctx = _FakeContextForTotal(guild)
    await cog.voicehours.callback(cog, ctx, "total", "<@&111>", "<@30>")  # type: ignore[union-attr]

    assert len(ctx.sent_messages) == 1
    msg = ctx.sent_messages[0]
    assert "TeamA + Charlie" in msg
    assert "1.50h" in msg  # 3600 + 1800 = 5400 = 1.50h
    assert "2 members" in msg


@pytest.mark.asyncio
async def test_voicehours_total_with_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Total respects the window tokens in the output label."""
    m1 = _FakeMemberWithBot(10)
    guild = _FakeGuildForTotal(1, [m1])

    totals = {10: 3600.0}
    cog = _make_total_cog(guild, totals)

    ctx = _FakeContextForTotal(guild)
    await cog.voicehours.callback(cog, ctx, "total", "everyone", "last", "1", "week")  # type: ignore[union-attr]

    assert len(ctx.sent_messages) == 1
    msg = ctx.sent_messages[0]
    assert "last 1 week" in msg
    assert "everyone" in msg


@pytest.mark.asyncio
async def test_voicehours_total_zero_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    """Total with no voice data shows 0.00h."""
    m1 = _FakeMemberWithBot(10)
    guild = _FakeGuildForTotal(1, [m1])

    cog = _make_total_cog(guild, {})

    ctx = _FakeContextForTotal(guild)
    await cog.voicehours.callback(cog, ctx, "total", "everyone")  # type: ignore[union-attr]

    assert len(ctx.sent_messages) == 1
    msg = ctx.sent_messages[0]
    assert "0.00h" in msg
    assert "1 members" in msg


@pytest.mark.asyncio
async def test_voicehours_total_unresolvable_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """When a target cannot be resolved, an error message is sent."""
    guild = _FakeGuildForTotal(1, [_FakeMemberWithBot(10)])
    cog = _make_total_cog(guild, {})

    async def _convert_role(self, ctx, arg):
        raise commands.BadArgument()

    async def _convert_member(self, ctx, arg):
        raise commands.BadArgument()

    monkeypatch.setattr(commands.RoleConverter, "convert", _convert_role)
    monkeypatch.setattr(commands.MemberConverter, "convert", _convert_member)

    ctx = _FakeContextForTotal(guild)
    await cog.voicehours.callback(cog, ctx, "total", "unknown_thing")  # type: ignore[union-attr]

    assert len(ctx.sent_messages) == 1
    assert "Could not resolve" in ctx.sent_messages[0]


@pytest.mark.asyncio
async def test_on_guild_channel_delete_closes_sessions_for_disconnected_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(voice_logging_module.discord, "VoiceChannel", _FakeVoiceChannel)

    member = _FakeMember(46)
    target_channel = _FakeVoiceChannel(1007, "Solo Room D", [])
    # Member is not in any voice channel right now, but has a persisted session
    guild = _FakeGuild(904, [target_channel], extra_members=[member])
    member.guild = guild
    member.voice = None

    # The listener reads channel.guild
    target_channel.guild = guild  # type: ignore[attr-defined]

    repo = _FakeRepo()
    cog = VoiceLoggingCog(
        bot=_FakeBot([guild]),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=_FakeConfigService(),  # type: ignore[arg-type]
    )
    cog._persisted_voice_sessions.add((guild.id, member.id))

    await cog.on_guild_channel_delete(target_channel)  # type: ignore[arg-type]

    assert len(repo.closed_calls) == 1
    assert repo.closed_calls[0][0] == guild.id
    assert repo.closed_calls[0][1] == member.id
    assert cog._persisted_voice_sessions == set()


@pytest.mark.asyncio
async def test_on_voice_state_update_closes_persisted_session_even_if_pending_task_not_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(voice_logging_module.discord, "VoiceChannel", _FakeVoiceChannel)

    member = _FakeMember(47)
    tracked_channel = _FakeVoiceChannel(1008, "Solo Room E", [member])
    guild = _FakeGuild(905, [tracked_channel])
    member.guild = guild
    member.voice = _FakeVoiceState(tracked_channel)

    repo = _FakeRepo()
    cog = VoiceLoggingCog(
        bot=_FakeBot([guild]),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=_FakeConfigService(),  # type: ignore[arg-type]
    )
    cog._stop_watchdog = lambda member_id: None  # type: ignore[assignment]
    cog._start_watchdog = lambda member_obj: None  # type: ignore[assignment]

    # Session is already persisted
    cog._persisted_voice_sessions.add((guild.id, member.id))

    # Add a dummy pending task that is not done (simulating a just-cancelled task)
    dummy_task = asyncio.create_task(asyncio.sleep(10))
    dummy_pending = voice_logging_module.PendingVoiceStart(
        guild_id=guild.id,
        member_id=member.id,
        member=member,
        channel_id=tracked_channel.id,
        channel_name=tracked_channel.name,
        started_at=datetime.now(tz=UTC),
    )
    cog._pending_voice_starts[(guild.id, member.id)] = (dummy_pending, dummy_task)

    # Member leaves the channel
    member.voice = None
    await cog.on_voice_state_update(
        member,  # type: ignore[arg-type]
        _FakeVoiceState(tracked_channel),  # type: ignore[arg-type]
        _FakeVoiceState(None),  # type: ignore[arg-type]
    )

    # Give the event loop a tick so the cancelled task can process its CancelledError
    await asyncio.sleep(0)

    # Task was cancelled by the handler
    assert dummy_task.cancelled() or dummy_task.done()

    # The persisted session MUST be closed despite the pending task existing
    assert len(repo.closed_calls) == 1
    assert repo.closed_calls[0][0] == guild.id
    assert repo.closed_calls[0][1] == member.id
    assert cog._persisted_voice_sessions == set()

    # Clean up the task
    try:
        await dummy_task
    except asyncio.CancelledError:
        pass

