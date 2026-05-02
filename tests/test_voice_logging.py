from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

import pytest

import cogs.voice_logging as voice_logging_module
from cogs.voice_logging import VoiceLoggingCog, WorkConfirmationResult
from db.repository import CoachConfig
from services.discord_output import DISCORD_MESSAGE_CHAR_LIMIT
from services.dynamic_voice import DynamicVoiceManager


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

    def get_member(self, member_id: int) -> None:
        return None


class _FakeRepoWithTotals:
    def __init__(self, totals: dict[int, float] | None = None) -> None:
        self._totals = totals or {}
        self._open_tracked_by_guild: dict[int, set[int]] = {}

    async def get_all(self, guild_id: int) -> list[Any]:
        return []

    async def get_tracked_voice_totals(
        self, guild_id: int, *, now: Any, since: Any
    ) -> dict[int, float]:
        return dict(self._totals)

    async def get_open_tracked_voice_member_ids(self, guild_id: int) -> set[int]:
        return set()


class _FakeConfigServiceWithMaxLines:
    async def get(self, guild_id: int) -> Any:
        class _Config:
            voice_check_interval_seconds = 3600
            voice_confirm_timeout_seconds = 60
            voicehours_max_lines = 50

        return _Config()

    async def get_text(self, guild_id: int, key: str) -> str:
        return ""


class _FakeContext:
    def __init__(self, guild: Any, author_id: int = 1) -> None:
        self.guild = guild
        self.sent_messages: list[str] = []

        class _Author:
            id = author_id
            display_name = "TestUser"

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

    async def _dm_failed(_: Any, __: int) -> WorkConfirmationResult:
        return WorkConfirmationResult.DM_FAILED

    cog._ask_still_working = _dm_failed  # type: ignore[method-assign]

    await cog._watchdog_loop(guild.id, member.id)

    assert len(coach.sent_messages) == 1
    assert "Worker" in coach.sent_messages[0]
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

    async def _dm_failed(_: Any, __: int) -> WorkConfirmationResult:
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

    async def _timed_out(_: Any, __: int) -> WorkConfirmationResult:
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

    ctx = _FakeContext(guild)
    await cog.voicehours.callback(cog, ctx, "tahzeeq", "2", "last", "1", "days")  # type: ignore[union-attr]

    output = "\n".join(ctx.sent_messages)
    assert "<@1>" in output
    assert "<@2>" not in output
    assert "Guest User" not in output


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
    cog = _make_cog_for_leaderboard(guild, totals)

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


@pytest.mark.asyncio
async def test_voicehours_teams_alias_behaves_like_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'teams' is an alias for 'roles' and uses the same team-prefix filter."""
    team_member = _FakeMemberWithBot(3)
    team_role = _FakeRole("Team Beta", [team_member])
    guild = _FakeGuildWithRoles(11, [team_role])

    totals = {3: 1800.0}
    cog = _make_cog_for_leaderboard(guild, totals)

    sent: list[str] = []

    async def _fake_send_chunks(ctx: Any, content: str) -> None:
        sent.append(content)

    monkeypatch.setattr(voice_logging_module, "send_context_text_chunks", _fake_send_chunks)

    ctx = _FakeContext(guild)
    await cog.voicehours.callback(cog, ctx, "teams")  # type: ignore[union-attr]

    assert len(sent) == 1
    assert "Team Role Voice Standings" in sent[0]


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
    cog = _make_cog_for_leaderboard(guild, totals)

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
