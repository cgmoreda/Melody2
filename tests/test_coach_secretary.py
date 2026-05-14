from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import cogs.coach_secretary as coach_module
import services.coach_secretary as secretary_module
from cogs.coach_secretary import CoachSecretaryCog
from db.repository import CoachConfig
from services.coach_secretary import CoachSecretary


class _FakeVoiceState:
    def __init__(self, channel: "_FakeVoiceChannel | None") -> None:
        self.channel = channel


class _FakeVoiceChannel:
    def __init__(self, channel_id: int, name: str) -> None:
        self.id = channel_id
        self.name = name
        self.mention = f"<#{channel_id}>"


class _FakeMember:
    def __init__(
        self,
        member_id: int,
        *,
        display_name: str | None = None,
        bot: bool = False,
        voice_channel: _FakeVoiceChannel | None = None,
    ) -> None:
        self.id = member_id
        self.display_name = display_name or f"member-{member_id}"
        self.mention = f"<@{member_id}>"
        self.bot = bot
        self.voice = _FakeVoiceState(voice_channel) if voice_channel is not None else None
        self.sent_messages: list[str] = []
        self.move_calls: list[tuple[_FakeVoiceChannel, str | None]] = []

    async def send(self, message: str, **kwargs: object) -> None:
        self.sent_messages.append(message)

    async def move_to(self, channel: _FakeVoiceChannel, *, reason: str | None = None) -> None:
        self.move_calls.append((channel, reason))
        self.voice = _FakeVoiceState(channel)

    def __str__(self) -> str:
        return self.display_name


class _FakeRole:
    def __init__(self, role_id: int, name: str, members: list[_FakeMember]) -> None:
        self.id = role_id
        self.name = name
        self.mention = f"<@&{role_id}>"
        self.members = members


class _FakeGuild:
    def __init__(
        self,
        guild_id: int,
        channels: list[_FakeVoiceChannel],
        members: list[_FakeMember] | None = None,
    ) -> None:
        self.id = guild_id
        self.name = f"guild-{guild_id}"
        self._channels = {channel.id: channel for channel in channels}
        self._members = {member.id: member for member in members or []}

    def get_channel(self, channel_id: int) -> _FakeVoiceChannel | None:
        return self._channels.get(channel_id)

    def get_member(self, member_id: int) -> _FakeMember | None:
        return self._members.get(member_id)


class _FakeContext:
    def __init__(self, guild: _FakeGuild, author: _FakeMember) -> None:
        self.guild = guild
        self.author = author
        self.sent_messages: list[str] = []
        self.sent_kwargs: list[dict[str, Any]] = []

    async def send(self, message: str | None = None, **kwargs: Any) -> None:
        self.sent_messages.append(message or "")
        self.sent_kwargs.append(kwargs)


class _FakeSecretary:
    def __init__(self, config: CoachConfig | None) -> None:
        self._config = config
        self.marked_summons: list[tuple[int, int]] = []

    async def get_config(self, guild_id: int) -> CoachConfig | None:
        return self._config

    async def save_config(self, config: CoachConfig) -> None:
        self._config = config

    async def remove_config(self, guild_id: int) -> bool:
        removed = self._config is not None
        self._config = None
        return removed

    def clear_notification(self, guild_id: int, member_id: int) -> None:
        return None

    def mark_summoned_member(self, guild_id: int, member_id: int) -> None:
        self.marked_summons.append((guild_id, member_id))


class _FakeCoachRepo:
    def __init__(self, config: CoachConfig | None) -> None:
        self._config = config

    async def get_coach_config(self, guild_id: int) -> CoachConfig | None:
        return self._config

    async def upsert_coach_config(self, config: CoachConfig) -> None:
        self._config = config

    async def delete_coach_config(self, guild_id: int) -> bool:
        removed = self._config is not None
        self._config = None
        return removed


@pytest.fixture(autouse=True)
def _patch_discord_types(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coach_module.discord, "Member", _FakeMember)
    monkeypatch.setattr(coach_module.discord, "Role", _FakeRole)
    monkeypatch.setattr(coach_module.discord, "VoiceChannel", _FakeVoiceChannel)
    monkeypatch.setattr(secretary_module.discord, "VoiceChannel", _FakeVoiceChannel)


@pytest.mark.asyncio
async def test_summon_role_moves_voice_members_and_dms_members_not_in_voice() -> None:
    office = _FakeVoiceChannel(10, "Coach Office")
    waiting = _FakeVoiceChannel(11, "Secretary")
    lounge = _FakeVoiceChannel(20, "Lounge")
    guild = _FakeGuild(100, [office, waiting, lounge])
    coach = _FakeMember(1, display_name="Coach", voice_channel=office)
    in_voice = _FakeMember(2, display_name="In Voice", voice_channel=lounge)
    not_in_voice = _FakeMember(3, display_name="No Voice")
    bot_member = _FakeMember(4, display_name="Bot", bot=True, voice_channel=lounge)
    role = _FakeRole(50, "Training", [in_voice, not_in_voice, bot_member])
    secretary = _FakeSecretary(
        CoachConfig(guild_id=guild.id, coach_id=coach.id, waiting_room_id=waiting.id, coach_channel_id=office.id)
    )
    cog = CoachSecretaryCog(
        bot=object(),  # type: ignore[arg-type]
        secretary=secretary,  # type: ignore[arg-type]
    )
    ctx = _FakeContext(guild, coach)

    await cog.summon.callback(cog, ctx, role)  # type: ignore[union-attr]

    assert in_voice.move_calls == [(office, f"Summoned by coach {coach} ({coach.id})")]
    assert "Coach Office" in not_in_voice.sent_messages[0]
    assert "Secretary" in not_in_voice.sent_messages[0]
    assert "10 minutes" in not_in_voice.sent_messages[0]
    assert guild.name in not_in_voice.sent_messages[0]
    assert secretary.marked_summons == [(guild.id, not_in_voice.id)]
    assert bot_member.move_calls == []
    assert bot_member.sent_messages == []
    assert ctx.sent_messages == ["Summon to <#10> complete. Moved: **1**. DM'd: **1**."]


@pytest.mark.asyncio
async def test_summon_member_dms_target_when_not_in_voice() -> None:
    office = _FakeVoiceChannel(10, "Coach Office")
    waiting = _FakeVoiceChannel(11, "Secretary")
    guild = _FakeGuild(100, [office, waiting])
    coach = _FakeMember(1, display_name="Coach")
    target = _FakeMember(2, display_name="Target")
    secretary = _FakeSecretary(
        CoachConfig(guild_id=guild.id, coach_id=coach.id, waiting_room_id=waiting.id, coach_channel_id=office.id)
    )
    cog = CoachSecretaryCog(
        bot=object(),  # type: ignore[arg-type]
        secretary=secretary,  # type: ignore[arg-type]
    )
    ctx = _FakeContext(guild, coach)

    await cog.summon.callback(cog, ctx, target)  # type: ignore[union-attr]

    assert target.move_calls == []
    assert target.sent_messages == [
        "Coach asked you to join the coach's office in guild-100: Coach Office. "
        "If you can only access Secretary, join it within 10 minutes and I'll move you in automatically."
    ]
    assert secretary.marked_summons == [(guild.id, target.id)]
    assert ctx.sent_messages == ["Summon to <#10> complete. DM'd: **1**."]


@pytest.mark.asyncio
async def test_summon_rejects_non_configured_coach() -> None:
    office = _FakeVoiceChannel(10, "Coach Office")
    lounge = _FakeVoiceChannel(20, "Lounge")
    guild = _FakeGuild(100, [office, lounge])
    author = _FakeMember(2, display_name="Not Coach")
    target = _FakeMember(3, display_name="Target", voice_channel=lounge)
    cog = CoachSecretaryCog(
        bot=object(),  # type: ignore[arg-type]
        secretary=_FakeSecretary(
            CoachConfig(guild_id=guild.id, coach_id=1, waiting_room_id=99, coach_channel_id=office.id)
        ),  # type: ignore[arg-type]
    )
    ctx = _FakeContext(guild, author)

    await cog.summon.callback(cog, ctx, target)  # type: ignore[union-attr]

    assert target.move_calls == []
    assert target.sent_messages == []
    assert ctx.sent_messages == ["Only the configured coach can use this command."]


@pytest.mark.asyncio
async def test_summon_requires_coach_configuration() -> None:
    office = _FakeVoiceChannel(10, "Coach Office")
    guild = _FakeGuild(100, [office])
    coach = _FakeMember(1, display_name="Coach")
    target = _FakeMember(2, display_name="Target")
    cog = CoachSecretaryCog(
        bot=object(),  # type: ignore[arg-type]
        secretary=_FakeSecretary(None),  # type: ignore[arg-type]
    )
    ctx = _FakeContext(guild, coach)

    await cog.summon.callback(cog, ctx, target)  # type: ignore[union-attr]

    assert target.sent_messages == []
    assert ctx.sent_messages == ["Coach secretary is not configured. Use !coach setup first."]


@pytest.mark.asyncio
async def test_bomb_mentions_target_requested_number_of_times() -> None:
    office = _FakeVoiceChannel(10, "Coach Office")
    guild = _FakeGuild(100, [office])
    coach = _FakeMember(1, display_name="Coach")
    target = _FakeMember(2, display_name="Target")
    secretary = _FakeSecretary(
        CoachConfig(guild_id=guild.id, coach_id=coach.id, waiting_room_id=99, coach_channel_id=office.id)
    )
    cog = CoachSecretaryCog(
        bot=object(),  # type: ignore[arg-type]
        secretary=secretary,  # type: ignore[arg-type]
    )
    ctx = _FakeContext(guild, coach)

    await cog.bomb.callback(cog, ctx, target, 3)  # type: ignore[union-attr]

    assert ctx.sent_messages == [target.mention, target.mention, target.mention]
    assert [kwargs["allowed_mentions"].to_dict() for kwargs in ctx.sent_kwargs] == [
        {"parse": ["users"]},
        {"parse": ["users"]},
        {"parse": ["users"]},
    ]


@pytest.mark.asyncio
async def test_bomb_rejects_non_configured_coach() -> None:
    office = _FakeVoiceChannel(10, "Coach Office")
    guild = _FakeGuild(100, [office])
    author = _FakeMember(2, display_name="Not Coach")
    target = _FakeMember(3, display_name="Target")
    cog = CoachSecretaryCog(
        bot=object(),  # type: ignore[arg-type]
        secretary=_FakeSecretary(
            CoachConfig(guild_id=guild.id, coach_id=1, waiting_room_id=99, coach_channel_id=office.id)
        ),  # type: ignore[arg-type]
    )
    ctx = _FakeContext(guild, author)

    await cog.bomb.callback(cog, ctx, target, 3)  # type: ignore[union-attr]

    assert ctx.sent_messages == ["Only the configured coach can use this command."]


@pytest.mark.asyncio
async def test_bomb_rejects_out_of_range_count() -> None:
    office = _FakeVoiceChannel(10, "Coach Office")
    guild = _FakeGuild(100, [office])
    coach = _FakeMember(1, display_name="Coach")
    target = _FakeMember(2, display_name="Target")
    cog = CoachSecretaryCog(
        bot=object(),  # type: ignore[arg-type]
        secretary=_FakeSecretary(
            CoachConfig(guild_id=guild.id, coach_id=coach.id, waiting_room_id=99, coach_channel_id=office.id)
        ),  # type: ignore[arg-type]
    )
    ctx = _FakeContext(guild, coach)

    await cog.bomb.callback(cog, ctx, target, 0)  # type: ignore[union-attr]

    assert ctx.sent_messages == ["Count must be between 1 and 20."]


@pytest.mark.asyncio
async def test_summoned_waiting_member_moves_directly_before_expiry() -> None:
    now = datetime(2026, 5, 11, 10, 0, tzinfo=UTC)
    office = _FakeVoiceChannel(10, "Coach Office")
    waiting = _FakeVoiceChannel(11, "Secretary")
    coach = _FakeMember(1, display_name="Coach")
    member = _FakeMember(2, display_name="Summoned", voice_channel=waiting)
    guild = _FakeGuild(100, [office, waiting], members=[coach, member])
    config = CoachConfig(guild_id=guild.id, coach_id=coach.id, waiting_room_id=waiting.id, coach_channel_id=office.id)
    secretary = CoachSecretary(_FakeCoachRepo(config), now_provider=lambda: now)  # type: ignore[arg-type]
    secretary.mark_summoned_member(guild.id, member.id)

    await secretary.handle_waiting_member(member, guild)  # type: ignore[arg-type]

    assert member.move_calls == [(office, "Summoned member joined waiting room")]
    assert coach.sent_messages == []


@pytest.mark.asyncio
async def test_summoned_waiting_member_bypass_expires_after_ten_minutes() -> None:
    current_time = datetime(2026, 5, 11, 10, 0, tzinfo=UTC)

    def _now() -> datetime:
        return current_time

    office = _FakeVoiceChannel(10, "Coach Office")
    waiting = _FakeVoiceChannel(11, "Secretary")
    coach = _FakeMember(1, display_name="Coach")
    member = _FakeMember(2, display_name="Summoned", voice_channel=waiting)
    guild = _FakeGuild(100, [office, waiting], members=[coach, member])
    config = CoachConfig(guild_id=guild.id, coach_id=coach.id, waiting_room_id=waiting.id, coach_channel_id=office.id)
    secretary = CoachSecretary(_FakeCoachRepo(config), now_provider=_now)  # type: ignore[arg-type]
    secretary.mark_summoned_member(guild.id, member.id)

    current_time += timedelta(minutes=10, seconds=1)
    await secretary.handle_waiting_member(member, guild)  # type: ignore[arg-type]

    assert member.move_calls == []
    assert len(coach.sent_messages) == 1
    assert "Summoned is in the waiting room" in coach.sent_messages[0]
