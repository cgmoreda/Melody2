from __future__ import annotations

from typing import Any

import pytest

import cogs.coach_secretary as coach_module
from cogs.coach_secretary import CoachSecretaryCog
from db.repository import CoachConfig


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
    def __init__(self, guild_id: int, channels: list[_FakeVoiceChannel]) -> None:
        self.id = guild_id
        self.name = f"guild-{guild_id}"
        self._channels = {channel.id: channel for channel in channels}

    def get_channel(self, channel_id: int) -> _FakeVoiceChannel | None:
        return self._channels.get(channel_id)


class _FakeContext:
    def __init__(self, guild: _FakeGuild, author: _FakeMember) -> None:
        self.guild = guild
        self.author = author
        self.sent_messages: list[str] = []

    async def send(self, message: str | None = None, **kwargs: Any) -> None:
        self.sent_messages.append(message or "")


class _FakeSecretary:
    def __init__(self, config: CoachConfig | None) -> None:
        self._config = config

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


@pytest.fixture(autouse=True)
def _patch_discord_types(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coach_module.discord, "Member", _FakeMember)
    monkeypatch.setattr(coach_module.discord, "Role", _FakeRole)
    monkeypatch.setattr(coach_module.discord, "VoiceChannel", _FakeVoiceChannel)


@pytest.mark.asyncio
async def test_summon_role_moves_voice_members_and_dms_members_not_in_voice() -> None:
    office = _FakeVoiceChannel(10, "Coach Office")
    lounge = _FakeVoiceChannel(20, "Lounge")
    guild = _FakeGuild(100, [office, lounge])
    coach = _FakeMember(1, display_name="Coach", voice_channel=office)
    in_voice = _FakeMember(2, display_name="In Voice", voice_channel=lounge)
    not_in_voice = _FakeMember(3, display_name="No Voice")
    bot_member = _FakeMember(4, display_name="Bot", bot=True, voice_channel=lounge)
    role = _FakeRole(50, "Training", [in_voice, not_in_voice, bot_member])
    cog = CoachSecretaryCog(
        bot=object(),  # type: ignore[arg-type]
        secretary=_FakeSecretary(
            CoachConfig(guild_id=guild.id, coach_id=coach.id, waiting_room_id=99, coach_channel_id=office.id)
        ),  # type: ignore[arg-type]
    )
    ctx = _FakeContext(guild, coach)

    await cog.summon.callback(cog, ctx, role)  # type: ignore[union-attr]

    assert in_voice.move_calls == [(office, f"Summoned by coach {coach} ({coach.id})")]
    assert "Coach Office" in not_in_voice.sent_messages[0]
    assert guild.name in not_in_voice.sent_messages[0]
    assert bot_member.move_calls == []
    assert bot_member.sent_messages == []
    assert ctx.sent_messages == ["Summon to <#10> complete. Moved: **1**. DM'd: **1**."]


@pytest.mark.asyncio
async def test_summon_member_dms_target_when_not_in_voice() -> None:
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

    await cog.summon.callback(cog, ctx, target)  # type: ignore[union-attr]

    assert target.move_calls == []
    assert target.sent_messages == ["Coach asked you to join the coach's office in guild-100: Coach Office."]
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
