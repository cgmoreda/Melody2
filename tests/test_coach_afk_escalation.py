from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
import discord

import cogs.voice_logging as voice_logging_module
from cogs.voice_logging import VoiceLoggingCog, AfkEscalationView, WorkConfirmationResult
from db.repository import CoachConfig

from tests.test_voice_logging import (
    _FakeMember,
    _FakeGuild,
    _FakeVoiceChannel,
    _FakeVoiceState,
    _FakeRepo,
    _FakeFastConfigService,
    _FakeCoachSecretary,
    _FakeBot,
)


class _InteractionResponseFake:
    def __init__(self):
        self.sent_messages = []
        self.edit_message_calls = []

    async def send_message(self, content: str, ephemeral: bool = False):
        self.sent_messages.append((content, ephemeral))

    async def edit_message(self, content: str, view: Any = None):
        self.edit_message_calls.append((content, view))


class _InteractionFake:
    def __init__(self, user):
        self.user = user
        self.response = _InteractionResponseFake()


@pytest.fixture(autouse=True)
def _patch_discord_types(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(voice_logging_module.discord, "VoiceChannel", _FakeVoiceChannel)
    monkeypatch.setattr(voice_logging_module.discord, "Member", _FakeMember)


@pytest.mark.asyncio
async def test_afk_escalation_triggers_coach_dm() -> None:
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

    async def _timed_out(*args: Any, **kwargs: Any) -> WorkConfirmationResult:
        return WorkConfirmationResult.TIMED_OUT

    cog._ask_still_working = _timed_out  # type: ignore[method-assign]

    await cog._watchdog_loop(guild.id, member.id)

    assert len(coach.sent_messages) == 1
    assert "<@10> was moved to the AFK room" in coach.sent_messages[0]
    assert "Solo Room A" in coach.sent_messages[0]
    assert member.move_calls == [(afk_channel, "Failed or missed solo-channel work check")]


@pytest.mark.asyncio
async def test_afk_escalation_coach_accepts_moves_user() -> None:
    member = _FakeMember(10, display_name="Worker")
    coach = _FakeMember(20, display_name="Coach")
    solo_channel = _FakeVoiceChannel(501, "Solo Room A", [member])
    afk_channel = _FakeVoiceChannel(599, "AFK", [])
    coach_office = _FakeVoiceChannel(2, "Coach Office", [coach])
    
    guild = _FakeGuild(100, [solo_channel, afk_channel, coach_office], extra_members=[coach], afk_channel=afk_channel)
    member.voice = _FakeVoiceState(afk_channel)
    
    config = CoachConfig(guild_id=100, coach_id=20, waiting_room_id=1, coach_channel_id=2)
    view = AfkEscalationView(member=member, guild=guild, original_channel=solo_channel, config=config)  # type: ignore[arg-type]
    
    interaction = _InteractionFake(user=coach)
    await view.children[0].callback(interaction)  # type: ignore
    
    assert member.move_calls == [(coach_office, "Coach escalated AFK warning")]
    assert len(interaction.response.edit_message_calls) == 1
    assert "Moved Worker to Coach Office" in interaction.response.edit_message_calls[0][0]
    assert view.children[0].disabled is True


@pytest.mark.asyncio
async def test_afk_escalation_coach_ignores() -> None:
    member = _FakeMember(10, display_name="Worker")
    coach = _FakeMember(20, display_name="Coach")
    solo_channel = _FakeVoiceChannel(501, "Solo Room A", [member])
    guild = _FakeGuild(100, [solo_channel], extra_members=[coach])
    
    config = CoachConfig(guild_id=100, coach_id=20, waiting_room_id=1, coach_channel_id=2)
    view = AfkEscalationView(member=member, guild=guild, original_channel=solo_channel, config=config)  # type: ignore[arg-type]
    
    interaction = _InteractionFake(user=coach)
    await view.children[1].callback(interaction)  # type: ignore
    
    assert member.move_calls == []
    assert len(interaction.response.edit_message_calls) == 1
    assert "Ignored AFK escalation for Worker." in interaction.response.edit_message_calls[0][0]


@pytest.mark.asyncio
async def test_afk_escalation_user_disconnected_before_action() -> None:
    member = _FakeMember(10, display_name="Worker")
    coach = _FakeMember(20, display_name="Coach")
    solo_channel = _FakeVoiceChannel(501, "Solo Room A", [])
    guild = _FakeGuild(100, [solo_channel], extra_members=[coach])
    
    # User left voice
    member.voice = None
    
    config = CoachConfig(guild_id=100, coach_id=20, waiting_room_id=1, coach_channel_id=2)
    view = AfkEscalationView(member=member, guild=guild, original_channel=solo_channel, config=config)  # type: ignore[arg-type]
    
    interaction = _InteractionFake(user=coach)
    await view.children[0].callback(interaction)  # type: ignore
    
    assert member.move_calls == []
    assert "Worker is no longer in voice." in interaction.response.edit_message_calls[0][0]


@pytest.mark.asyncio
async def test_afk_escalation_missing_office_channel() -> None:
    member = _FakeMember(10, display_name="Worker")
    coach = _FakeMember(20, display_name="Coach")
    solo_channel = _FakeVoiceChannel(501, "Solo Room A", [member])
    afk_channel = _FakeVoiceChannel(599, "AFK", [])
    guild = _FakeGuild(100, [solo_channel, afk_channel], extra_members=[coach], afk_channel=afk_channel)
    member.voice = _FakeVoiceState(afk_channel)
    
    config = CoachConfig(guild_id=100, coach_id=20, waiting_room_id=1, coach_channel_id=999) # Non-existent
    view = AfkEscalationView(member=member, guild=guild, original_channel=solo_channel, config=config)  # type: ignore[arg-type]
    
    interaction = _InteractionFake(user=coach)
    await view.children[0].callback(interaction)  # type: ignore
    
    assert member.move_calls == []
    assert "Coach office was deleted." in interaction.response.edit_message_calls[0][0]


@pytest.mark.asyncio
async def test_afk_escalation_permission_failure() -> None:
    class _DummyResponse:
        status = 403
        reason = "Forbidden"

    class ForbiddenFakeMember(_FakeMember):
        async def move_to(self, channel: Any, *, reason: str | None = None) -> None:
            raise discord.Forbidden(_DummyResponse(), 'missing permissions') # type: ignore

    member = ForbiddenFakeMember(10, display_name="Worker")
    coach = _FakeMember(20, display_name="Coach")
    solo_channel = _FakeVoiceChannel(501, "Solo Room A", [member])
    afk_channel = _FakeVoiceChannel(599, "AFK", [])
    coach_office = _FakeVoiceChannel(2, "Coach Office", [coach])
    
    guild = _FakeGuild(100, [solo_channel, afk_channel, coach_office], extra_members=[coach], afk_channel=afk_channel)
    member.voice = _FakeVoiceState(afk_channel)
    member.guild = guild  # type: ignore
    guild._members[member.id] = member  # type: ignore
    
    config = CoachConfig(guild_id=100, coach_id=20, waiting_room_id=1, coach_channel_id=2)
    view = AfkEscalationView(member=member, guild=guild, original_channel=solo_channel, config=config)  # type: ignore[arg-type]
    
    interaction = _InteractionFake(user=coach)
    await view.children[0].callback(interaction)  # type: ignore
    
    assert "Missing permissions to move that member." in interaction.response.edit_message_calls[0][0]
