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


class _FakePermissions:
    def __init__(self, *, administrator: bool = False) -> None:
        self.administrator = administrator


class _FakeRole:
    def __init__(self, role_id: int, name: str, *, administrator: bool = False) -> None:
        self.id = role_id
        self.name = name
        self.permissions = _FakePermissions(administrator=administrator)


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

    def overwrites_for(self, target: Any) -> _FakeOverwrite:
        """Return the overwrite for *target*, or a neutral overwrite."""
        for key, value in self.overwrites.items():
            if getattr(key, "id", None) == getattr(target, "id", None):
                return value
        return _FakeOverwrite()


class _FakeGuild:
    def __init__(
        self,
        guild_id: int,
        voice_channels: list[_FakeVoiceChannel],
        *,
        roles: list[_FakeRole] | None = None,
    ) -> None:
        self.id = guild_id
        self.voice_channels = voice_channels
        if roles and roles[0].name == "@everyone":
            self.default_role = roles[0]
        else:
            self.default_role = _FakeRole(0, "@everyone")
        self.roles = roles or [self.default_role]
        if self.default_role not in self.roles:
            self.roles.insert(0, self.default_role)


class _FakeMember:
    def __init__(
        self,
        member_id: int,
        *,
        roles: list[_FakeRole] | None = None,
        guild: Any = None,
        administrator: bool = False,
    ) -> None:
        self.id = member_id
        self.roles: list[_FakeRole] = roles or []
        self.guild = guild
        self.display_name = f"member-{member_id}"
        self.bot = False
        self.guild_permissions = _FakePermissions(administrator=administrator)


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
# check_access
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_access_duo_allows_member_without_matching_role() -> None:
    """Duo channels are open even when the member has no matching Team role."""
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

    member = _FakeMember(7, roles=[_FakeRole(10, "Team Beta")])
    channel = _FakeVoiceChannel(channel_id, "Alpha Duo #1")

    result = await manager.check_access(member, channel)  # type: ignore[arg-type]
    assert result is True


@pytest.mark.asyncio
async def test_check_access_team_blocks_member_without_matching_role() -> None:
    """Team channels block members without a matching Team role."""
    manager = DynamicVoiceManager()
    channel_id = 102

    tracked = TrackedChannel(
        channel_id=channel_id,
        guild_id=1,
        channel_type=ChannelType.TEAM,
        creator_id=0,  # unknown after restart
        label="Alpha Team",
        number=1,
    )
    manager._channels[1] = {channel_id: tracked}

    member = _FakeMember(8, roles=[_FakeRole(11, "Team Beta")])
    channel = _FakeVoiceChannel(channel_id, "Alpha Team #1")

    result = await manager.check_access(member, channel)  # type: ignore[arg-type]
    assert result is False


@pytest.mark.asyncio
async def test_check_access_team_allows_member_with_matching_role() -> None:
    """When creator_id==0, role-based check grants Team access to matching-role members."""
    manager = DynamicVoiceManager()
    channel_id = 101

    tracked = TrackedChannel(
        channel_id=channel_id,
        guild_id=1,
        channel_type=ChannelType.TEAM,
        creator_id=0,  # unknown after restart
        label="Alpha Team",
        number=1,
    )
    manager._channels[1] = {channel_id: tracked}

    member = _FakeMember(8, roles=[_FakeRole(11, "Team Alpha")])
    channel = _FakeVoiceChannel(channel_id, "Alpha Team #1")

    result = await manager.check_access(member, channel)  # type: ignore[arg-type]
    assert result is True


@pytest.mark.asyncio
async def test_check_access_known_creator_shortcut_bypasses_role_check() -> None:
    """When creator_id is known, the creator is allowed even without the Team role."""
    manager = DynamicVoiceManager()
    channel_id = 103

    tracked = TrackedChannel(
        channel_id=channel_id,
        guild_id=1,
        channel_type=ChannelType.TEAM,
        creator_id=99,
        label="Alpha Team",
        number=1,
    )
    manager._channels[1] = {channel_id: tracked}

    # Creator has no Team role at all
    member = _FakeMember(99, roles=[])
    channel = _FakeVoiceChannel(channel_id, "Alpha Team #1")

    result = await manager.check_access(member, channel)  # type: ignore[arg-type]
    assert result is True


# ---------------------------------------------------------------------------
# check_access - coach/admin bypass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_access_team_allows_member_with_coach_role() -> None:
    """Coach roles bypass Team channel access restrictions."""
    manager = DynamicVoiceManager()
    channel_id = 104

    tracked = TrackedChannel(
        channel_id=channel_id,
        guild_id=1,
        channel_type=ChannelType.TEAM,
        creator_id=0,
        label="Alpha Team",
        number=1,
    )
    manager._channels[1] = {channel_id: tracked}

    member = _FakeMember(10, roles=[_FakeRole(12, "Coach")])
    channel = _FakeVoiceChannel(channel_id, "Alpha Team #1")

    result = await manager.check_access(member, channel)  # type: ignore[arg-type]
    assert result is True


@pytest.mark.asyncio
async def test_check_access_team_allows_member_with_admin_permissions() -> None:
    """Administrators bypass Team channel access restrictions."""
    manager = DynamicVoiceManager()
    channel_id = 105

    tracked = TrackedChannel(
        channel_id=channel_id,
        guild_id=1,
        channel_type=ChannelType.TEAM,
        creator_id=0,
        label="Alpha Team",
        number=1,
    )
    manager._channels[1] = {channel_id: tracked}

    member = _FakeMember(11, roles=[], administrator=True)
    channel = _FakeVoiceChannel(channel_id, "Alpha Team #1")

    result = await manager.check_access(member, channel)  # type: ignore[arg-type]
    assert result is True


# ---------------------------------------------------------------------------
# rebuild_state - creator_id inferred from member overwrite
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


def test_build_overwrites_allows_coach_and_admin_roles_for_team_channel() -> None:
    """Team channel overwrites keep coach/admin roles able to connect."""
    everyone = _FakeRole(0, "@everyone")
    coach = _FakeRole(20, "Coach")
    admin = _FakeRole(21, "Admin", administrator=True)
    team = _FakeRole(22, "Team Alpha")
    guild = _FakeGuild(1, [], roles=[everyone, coach, admin, team])
    member = _FakeMember(1, roles=[team], guild=guild)

    manager = DynamicVoiceManager()
    overwrites = manager._build_overwrites(member, ChannelType.TEAM)  # type: ignore[arg-type]

    assert overwrites[everyone].connect is False
    assert overwrites[team].connect is True
    assert overwrites[coach].connect is True
    assert overwrites[admin].connect is True


# ---------------------------------------------------------------------------
# Invite channel – check_access
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_access_invite_allows_creator() -> None:
    """Creator of an invite channel is always allowed."""
    manager = DynamicVoiceManager()
    channel_id = 200

    tracked = TrackedChannel(
        channel_id=channel_id,
        guild_id=1,
        channel_type=ChannelType.INVITE,
        creator_id=50,
        label="Alpha Invite",
        number=1,
    )
    manager._channels[1] = {channel_id: tracked}

    member = _FakeMember(50, roles=[])
    channel = _FakeVoiceChannel(channel_id, "Alpha Invite #1")

    result = await manager.check_access(member, channel)  # type: ignore[arg-type]
    assert result is True


@pytest.mark.asyncio
async def test_check_access_invite_blocks_uninvited_member() -> None:
    """Members without a connect=True overwrite are blocked from invite channels."""
    manager = DynamicVoiceManager()
    channel_id = 201

    tracked = TrackedChannel(
        channel_id=channel_id,
        guild_id=1,
        channel_type=ChannelType.INVITE,
        creator_id=50,
        label="Alpha Invite",
        number=1,
    )
    manager._channels[1] = {channel_id: tracked}

    member = _FakeMember(60, roles=[])  # no overwrite
    channel = _FakeVoiceChannel(channel_id, "Alpha Invite #1")

    result = await manager.check_access(member, channel)  # type: ignore[arg-type]
    assert result is False


@pytest.mark.asyncio
async def test_check_access_invite_allows_invited_member() -> None:
    """Members with a connect=True overwrite are allowed into invite channels."""
    manager = DynamicVoiceManager()
    channel_id = 202

    tracked = TrackedChannel(
        channel_id=channel_id,
        guild_id=1,
        channel_type=ChannelType.INVITE,
        creator_id=50,
        label="Alpha Invite",
        number=1,
    )
    manager._channels[1] = {channel_id: tracked}

    invited_target = _FakeMemberTarget(70)
    member = _FakeMember(70, roles=[])
    channel = _FakeVoiceChannel(
        channel_id,
        "Alpha Invite #1",
        overwrites={invited_target: _FakeOverwrite(connect=True)},
    )

    result = await manager.check_access(member, channel)  # type: ignore[arg-type]
    assert result is True


@pytest.mark.asyncio
async def test_check_access_invite_allows_coach() -> None:
    """Coaches bypass invite channel restrictions."""
    manager = DynamicVoiceManager()
    channel_id = 203

    tracked = TrackedChannel(
        channel_id=channel_id,
        guild_id=1,
        channel_type=ChannelType.INVITE,
        creator_id=50,
        label="Alpha Invite",
        number=1,
    )
    manager._channels[1] = {channel_id: tracked}

    member = _FakeMember(80, roles=[_FakeRole(12, "Coach")])
    channel = _FakeVoiceChannel(channel_id, "Alpha Invite #1")

    result = await manager.check_access(member, channel)  # type: ignore[arg-type]
    assert result is True


# ---------------------------------------------------------------------------
# Invite channel – _build_overwrites
# ---------------------------------------------------------------------------


def test_build_overwrites_invite_allows_only_creator() -> None:
    """Invite channel overwrites deny @everyone and allow only the creator."""
    everyone = _FakeRole(0, "@everyone")
    coach = _FakeRole(20, "Coach")
    team = _FakeRole(22, "Team Alpha")
    guild = _FakeGuild(1, [], roles=[everyone, coach, team])
    member = _FakeMember(1, roles=[team], guild=guild)

    manager = DynamicVoiceManager()
    overwrites = manager._build_overwrites(member, ChannelType.INVITE)  # type: ignore[arg-type]

    assert overwrites[everyone].connect is False
    assert overwrites[member].connect is True
    assert overwrites[coach].connect is True
    # Team role should NOT be in overwrites for invite channels
    assert team not in overwrites


# ---------------------------------------------------------------------------
# Invite channel – _has_connect_overwrite
# ---------------------------------------------------------------------------


def test_has_connect_overwrite_true() -> None:
    """Returns True when member has connect=True overwrite."""
    target = _FakeMemberTarget(42)
    channel = _FakeVoiceChannel(
        1, "Test Invite #1",
        overwrites={target: _FakeOverwrite(connect=True)},
    )
    member = _FakeMember(42)

    assert DynamicVoiceManager._has_connect_overwrite(channel, member) is True  # type: ignore[arg-type]


def test_has_connect_overwrite_false_when_no_overwrite() -> None:
    """Returns False when member has no overwrite."""
    channel = _FakeVoiceChannel(1, "Test Invite #1")
    member = _FakeMember(42)

    assert DynamicVoiceManager._has_connect_overwrite(channel, member) is False  # type: ignore[arg-type]


def test_has_connect_overwrite_false_when_connect_false() -> None:
    """Returns False when member overwrite has connect=False."""
    target = _FakeMemberTarget(42)
    channel = _FakeVoiceChannel(
        1, "Test Invite #1",
        overwrites={target: _FakeOverwrite(connect=False)},
    )
    member = _FakeMember(42)

    assert DynamicVoiceManager._has_connect_overwrite(channel, member) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Invite channel – _parse_channel_name / _build_label
# ---------------------------------------------------------------------------


def test_parse_channel_name_invite() -> None:
    """Invite channel names are parsed correctly."""
    result = DynamicVoiceManager._parse_channel_name("Alpha Invite #3")
    assert result == (ChannelType.INVITE, "Alpha Invite", 3)


def test_is_dynamic_channel_name_invite() -> None:
    """is_dynamic_channel_name recognises invite channel patterns."""
    assert DynamicVoiceManager.is_dynamic_channel_name("Alpha Invite #1") is True
    assert DynamicVoiceManager.is_dynamic_channel_name("Invite #1") is False  # no group name


def test_build_label_invite_with_team_role() -> None:
    """Invite label uses Team role name when available."""
    manager = DynamicVoiceManager()
    member = _FakeMember(1, roles=[_FakeRole(10, "Team Alpha")])
    label = manager._build_label(member, ChannelType.INVITE)  # type: ignore[arg-type]
    assert label == "Alpha Invite"


def test_build_label_invite_fallback_to_display_name() -> None:
    """Invite label falls back to display_name when no role matches."""
    manager = DynamicVoiceManager()
    member = _FakeMember(1, roles=[])
    label = manager._build_label(member, ChannelType.INVITE)  # type: ignore[arg-type]
    assert label == "member-1 Invite"
