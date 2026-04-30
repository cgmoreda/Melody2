"""Tests for DynamicVoiceManager – creator inference and access control."""

from __future__ import annotations

import contextlib
from typing import Any, Generator

import pytest

import services.dynamic_voice as dv_module
from services.dynamic_voice import ChannelType, DynamicVoiceManager, TrackedChannel


# ---------------------------------------------------------------------------
# Minimal fakes
# ---------------------------------------------------------------------------


class _FakeRole:
    def __init__(self, role_id: int, name: str) -> None:
        self.id = role_id
        self.name = name


class _FakeOverwrite:
    def __init__(self, *, connect: bool | None = None) -> None:
        self.connect = connect


class _FakeMemberTarget:
    """Used as a discord.Member key in channel.overwrites."""

    def __init__(self, member_id: int) -> None:
        self.id = member_id


class _FakeVoiceChannel:
    def __init__(
        self,
        channel_id: int,
        name: str,
        *,
        members: list[Any] | None = None,
        overwrites: dict[Any, _FakeOverwrite] | None = None,
    ) -> None:
        self.id = channel_id
        self.name = name
        self.members = members or []
        self.overwrites = overwrites or {}


class _FakeGuild:
    def __init__(self, guild_id: int, voice_channels: list[_FakeVoiceChannel]) -> None:
        self.id = guild_id
        self.voice_channels = voice_channels
        self.default_role = _FakeRole(0, "@everyone")


class _FakeMember:
    def __init__(
        self,
        member_id: int,
        *,
        roles: list[_FakeRole] | None = None,
        guild: Any = None,
    ) -> None:
        self.id = member_id
        self.roles: list[_FakeRole] = roles or []
        self.guild = guild
        self.display_name = f"member-{member_id}"
        self.bot = False


# ---------------------------------------------------------------------------
# Shared fixture: make _FakeMemberTarget pass isinstance(target, discord.Member)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _patch_discord_member() -> Generator[None, None, None]:
    """Temporarily replace discord.Member with _FakeMemberTarget for isinstance checks."""
    original = dv_module.discord.Member
    dv_module.discord.Member = _FakeMemberTarget  # type: ignore[assignment]
    try:
        yield
    finally:
        dv_module.discord.Member = original


# ---------------------------------------------------------------------------
# _infer_creator_id_from_channel
# ---------------------------------------------------------------------------


def test_infer_creator_id_returns_member_with_connect_true() -> None:
    """Member overwrite with connect=True is identified as the creator."""
    member_target = _FakeMemberTarget(42)
    channel = _FakeVoiceChannel(
        1,
        "Test Duo #1",
        overwrites={member_target: _FakeOverwrite(connect=True)},
    )
    with _patch_discord_member():
        result = DynamicVoiceManager._infer_creator_id_from_channel(channel)  # type: ignore[arg-type]

    assert result == 42


def test_infer_creator_id_returns_zero_when_no_member_overwrite() -> None:
    """Returns 0 when only role overwrites exist (creator had a Team role)."""
    role_target = _FakeRole(99, "Team Alpha")
    channel = _FakeVoiceChannel(
        2,
        "Alpha Team #1",
        overwrites={role_target: _FakeOverwrite(connect=True)},
    )
    with _patch_discord_member():
        result = DynamicVoiceManager._infer_creator_id_from_channel(channel)  # type: ignore[arg-type]

    assert result == 0


def test_infer_creator_id_ignores_connect_false_overwrite() -> None:
    """A member overwrite with connect=False (deny) is not treated as creator."""
    member_target = _FakeMemberTarget(55)
    channel = _FakeVoiceChannel(
        3,
        "Test Duo #1",
        overwrites={member_target: _FakeOverwrite(connect=False)},
    )
    with _patch_discord_member():
        result = DynamicVoiceManager._infer_creator_id_from_channel(channel)  # type: ignore[arg-type]

    assert result == 0


# ---------------------------------------------------------------------------
# check_access – creator_id == 0 (unknown after restart with Team role)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_access_unknown_creator_allows_member_with_matching_role() -> None:
    """When creator_id==0, role-based check still grants access to matching-role members."""
    manager = DynamicVoiceManager()
    channel_id = 100

    tracked = TrackedChannel(
        channel_id=channel_id,
        guild_id=1,
        channel_type=ChannelType.DUO,
        creator_id=0,  # unknown after restart
        label="Alpha Duo",
        number=1,
    )
    manager._channels[1] = {channel_id: tracked}

    member = _FakeMember(7, roles=[_FakeRole(10, "Team Alpha")])
    channel = _FakeVoiceChannel(channel_id, "Alpha Duo #1")

    result = await manager.check_access(member, channel)  # type: ignore[arg-type]
    assert result is True


@pytest.mark.asyncio
async def test_check_access_unknown_creator_blocks_member_without_matching_role() -> None:
    """When creator_id==0, a member with no matching Team role is blocked."""
    manager = DynamicVoiceManager()
    channel_id = 101

    tracked = TrackedChannel(
        channel_id=channel_id,
        guild_id=1,
        channel_type=ChannelType.DUO,
        creator_id=0,  # unknown after restart
        label="Alpha Duo",
        number=1,
    )
    manager._channels[1] = {channel_id: tracked}

    member = _FakeMember(8, roles=[_FakeRole(11, "Team Beta")])
    channel = _FakeVoiceChannel(channel_id, "Alpha Duo #1")

    result = await manager.check_access(member, channel)  # type: ignore[arg-type]
    assert result is False


@pytest.mark.asyncio
async def test_check_access_known_creator_shortcut_bypasses_role_check() -> None:
    """When creator_id is known, the creator is allowed even without the Team role."""
    manager = DynamicVoiceManager()
    channel_id = 102

    tracked = TrackedChannel(
        channel_id=channel_id,
        guild_id=1,
        channel_type=ChannelType.DUO,
        creator_id=99,
        label="Alpha Duo",
        number=1,
    )
    manager._channels[1] = {channel_id: tracked}

    # Creator has no Team role at all
    member = _FakeMember(99, roles=[])
    channel = _FakeVoiceChannel(channel_id, "Alpha Duo #1")

    result = await manager.check_access(member, channel)  # type: ignore[arg-type]
    assert result is True


# ---------------------------------------------------------------------------
# rebuild_state – creator_id inferred from member overwrite
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rebuild_state_infers_creator_id_from_member_overwrite() -> None:
    """rebuild_state() recovers creator_id from a member-specific connect=True overwrite."""
    member_target = _FakeMemberTarget(42)
    vc = _FakeVoiceChannel(
        200,
        "Test Duo #1",
        members=[_FakeMemberTarget(42)],  # channel is non-empty so not deleted
        overwrites={member_target: _FakeOverwrite(connect=True)},
    )
    guild = _FakeGuild(1, [vc])

    manager = DynamicVoiceManager()
    with _patch_discord_member():
        await manager.rebuild_state(guild)  # type: ignore[arg-type]

    info = manager.get_tracked_info(200)
    assert info is not None
    assert info.creator_id == 42


@pytest.mark.asyncio
async def test_rebuild_state_sets_creator_id_zero_when_no_member_overwrite() -> None:
    """rebuild_state() sets creator_id=0 when only role overwrites exist."""
    role_target = _FakeRole(99, "Team Alpha")
    vc = _FakeVoiceChannel(
        201,
        "Alpha Duo #1",
        members=[_FakeMemberTarget(5)],
        overwrites={role_target: _FakeOverwrite(connect=True)},
    )
    guild = _FakeGuild(1, [vc])

    manager = DynamicVoiceManager()
    with _patch_discord_member():
        await manager.rebuild_state(guild)  # type: ignore[arg-type]

    info = manager.get_tracked_info(201)
    assert info is not None
    assert info.creator_id == 0

