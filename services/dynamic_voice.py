"""Dynamic voice channel management service."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import discord

logger = logging.getLogger(__name__)


class ChannelType(Enum):
    SOLO = "solo"
    DUO = "duo"
    TEAM = "team"


ENTRY_CHANNEL_NAMES: dict[str, ChannelType] = {
    "➕ | Solo (new)": ChannelType.SOLO,
    "➕ | Duo (new)": ChannelType.DUO,
    "➕ | Team (new)": ChannelType.TEAM,
}

USER_LIMITS: dict[ChannelType, int] = {
    ChannelType.SOLO: 1,
    ChannelType.DUO: 2,
    ChannelType.TEAM: 3,
}

_SOLO_RE = re.compile(r"^solo #(\d+)$", re.IGNORECASE)
_DUO_RE = re.compile(r"^(.+) Duo #(\d+)$")
_TEAM_RE = re.compile(r"^(.+) Team #(\d+)$")

_DELETE_DELAY_SECONDS = 2.0


@dataclass(slots=True)
class TrackedChannel:
    """Metadata for a dynamically-created voice channel."""

    channel_id: int
    guild_id: int
    channel_type: ChannelType
    creator_id: int
    label: str  # e.g. "solo", "Assiut Duo", "Assiut Team"
    number: int  # the N in #N


class DynamicVoiceManager:
    """Creates, tracks, and cleans up dynamic voice channels."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._channels: dict[int, dict[int, TrackedChannel]] = {}
        self._delete_tasks: dict[int, asyncio.Task[None]] = {}

    # ── public API ──────────────────────────────────────────────

    def is_entry_channel(self, channel_name: str) -> Optional[ChannelType]:
        """Return the ChannelType if *channel_name* is an entry channel."""
        return ENTRY_CHANNEL_NAMES.get(channel_name)

    def is_tracked(self, channel_id: int) -> bool:
        """Return whether *channel_id* is a dynamically-created channel."""
        for guild_channels in self._channels.values():
            if channel_id in guild_channels:
                return True
        return False

    @staticmethod
    def is_dynamic_channel_name(name: str) -> bool:
        """Return True if *name* matches a dynamically-created channel name pattern.

        This check is based solely on the channel name, so it works even before
        :py:meth:`rebuild_state` has populated the in-memory tracking state.
        """
        return DynamicVoiceManager._parse_channel_name(name) is not None

    def get_tracked_info(self, channel_id: int) -> Optional[TrackedChannel]:
        """Return TrackedChannel metadata, or None if not tracked."""
        for guild_channels in self._channels.values():
            info = guild_channels.get(channel_id)
            if info is not None:
                return info
        return None

    async def check_access(self, member: discord.Member, channel: discord.VoiceChannel) -> bool:
        """Return True if *member* is allowed in the dynamic channel.

        Solo channels: always allowed.
        Duo channels: always allowed.
        Coaches/admins are always allowed.
        Team channel creators are always allowed when known.
        Team channels otherwise require a role whose 'Team ' suffix
        matches the channel label (e.g. label 'Assiut Team' requires 'Team Assiut').
        """
        info = self.get_tracked_info(channel.id)
        if info is None:
            return True  # not tracked → no restriction
        if self._is_coach_or_admin(member):
            return True
        if info.channel_type in (ChannelType.SOLO, ChannelType.DUO):
            return True
        # Only apply the creator shortcut when the creator is known (creator_id > 0).
        # creator_id == 0 is set by rebuild_state() when the creator could not be inferred
        # from channel overwrites; in that case we fall through to the role-based check.
        if info.creator_id != 0 and member.id == info.creator_id:
            return True

        # Extract the group name from the label (e.g. 'Assiut Duo' → 'Assiut')
        required_group = self._extract_group_from_label(info.label, info.channel_type)
        if required_group is None:
            return True  # fallback: allow if we can't determine the group

        member_group = self._get_role_name(member, "Team ")
        return member_group is not None and member_group.lower() == required_group.lower()

    def cancel_pending_delete(self, channel_id: int) -> None:
        """Cancel a pending deletion task for a channel, if one exists."""
        task = self._delete_tasks.pop(channel_id, None)
        if task is not None:
            task.cancel()

    async def handle_join(
        self,
        member: discord.Member,
        entry_channel: discord.VoiceChannel,
        channel_type: ChannelType,
    ) -> None:
        """Create a dynamic channel and move *member* into it."""
        async with self._lock:
            label = self._build_label(member, channel_type)

            number = self._next_number(member.guild.id, label)
            channel_name = f"{label} #{number}"
            user_limit = USER_LIMITS[channel_type]
            category = entry_channel.category

            overwrites = self._build_overwrites(member, channel_type)

            try:
                new_channel = await member.guild.create_voice_channel(
                    name=channel_name,
                    user_limit=user_limit,
                    category=category,
                    overwrites=overwrites,
                    reason=f"Dynamic {channel_type.value} channel for {member.display_name}",
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.error("Failed to create channel %r: %s", channel_name, exc)
                return

            tracked = TrackedChannel(
                channel_id=new_channel.id,
                guild_id=member.guild.id,
                channel_type=channel_type,
                creator_id=member.id,
                label=label,
                number=number,
            )
            self._channels.setdefault(member.guild.id, {})[new_channel.id] = tracked
            logger.info(
                "Created %s (id=%s, type=%s) for member %s",
                channel_name,
                new_channel.id,
                channel_type.value,
                member.id,
            )

        # Move outside the lock to avoid holding it during the API call
        try:
            await member.move_to(new_channel, reason="Dynamic voice channel assignment")
            logger.info("Moved member %s to %s", member.id, channel_name)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.error("Failed to move member %s to %s: %s", member.id, channel_name, exc)
            await self._schedule_delete(new_channel)

    async def handle_leave(self, channel: discord.VoiceChannel) -> None:
        """Schedule deletion of a tracked channel if it is empty."""
        if not self.is_tracked(channel.id):
            return
        if len(channel.members) == 0:
            await self._schedule_delete(channel)

    async def rebuild_state(self, guild: discord.Guild) -> None:
        """Scan guild channels on startup; rebuild tracking and delete orphans."""
        async with self._lock:
            guild_channels = self._channels.setdefault(guild.id, {})
            for vc in guild.voice_channels:
                parsed = self._parse_channel_name(vc.name)
                if parsed is None:
                    continue

                channel_type, label, number = parsed

                if len(vc.members) == 0:
                    try:
                        await vc.delete(reason="Orphan dynamic channel cleanup on startup")
                        logger.info("Deleted orphan channel %s (id=%s)", vc.name, vc.id)
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        logger.error("Failed to delete orphan %s: %s", vc.name, exc)
                    continue

                tracked = TrackedChannel(
                    channel_id=vc.id,
                    guild_id=guild.id,
                    channel_type=channel_type,
                    creator_id=self._infer_creator_id_from_channel(vc),
                    label=label,
                    number=number,
                )
                guild_channels[vc.id] = tracked
                logger.info("Recovered tracked channel %s (id=%s)", vc.name, vc.id)

    # ── internals ───────────────────────────────────────────────

    @staticmethod
    def _infer_creator_id_from_channel(channel: discord.VoiceChannel) -> int:
        """Try to recover the creator's Discord ID from channel permission overwrites.

        When a Duo/Team channel is created for a member who has **no** Team role,
        ``_build_overwrites`` adds a member-specific ``connect=True`` overwrite as a
        fallback.  We can recover that member ID on restart by looking for the first
        ``discord.Member`` target with an explicit ``connect=True`` overwrite.

        Returns 0 when no such overwrite is found (e.g., the creator had a Team role
        and was covered by the role overwrite rather than a member overwrite).
        """
        for target, overwrite in channel.overwrites.items():
            if isinstance(target, discord.Member) and overwrite.connect is True:
                return target.id
        return 0

    @staticmethod
    def _get_role_name(member: discord.Member, prefix: str) -> Optional[str]:
        """Return the name portion after *prefix* from the member's roles (case-insensitive)."""
        prefix_lower = prefix.lower()
        for role in member.roles:
            if role.name.lower().startswith(prefix_lower):
                return role.name[len(prefix):]
        return None

    @staticmethod
    def _role_is_coach_or_admin(role: discord.Role) -> bool:
        """Return True for coach roles or roles with administrator permissions."""
        role_name = getattr(role, "name", "").casefold()
        if "coach" in role_name:
            return True
        permissions = getattr(role, "permissions", None)
        return bool(getattr(permissions, "administrator", False))

    @classmethod
    def _is_coach_or_admin(cls, member: discord.Member) -> bool:
        """Return True when a member should bypass dynamic voice access checks."""
        guild_permissions = getattr(member, "guild_permissions", None)
        if getattr(guild_permissions, "administrator", False):
            return True
        return any(cls._role_is_coach_or_admin(role) for role in getattr(member, "roles", ()))

    @staticmethod
    def _extract_group_from_label(label: str, channel_type: ChannelType) -> Optional[str]:
        """Extract the group name from a channel label.

        'Assiut Duo' → 'Assiut', 'Cairo Team' → 'Cairo'.
        Returns None for solo or unrecognised labels.
        """
        if channel_type is ChannelType.DUO:
            suffix = " Duo"
        elif channel_type is ChannelType.TEAM:
            suffix = " Team"
        else:
            return None
        if label.endswith(suffix):
            return label[: -len(suffix)]
        return None

    def _build_label(self, member: discord.Member, channel_type: ChannelType) -> str:
        """Build the base label for a channel (without the #N suffix).

        Fallback chain:
        - Solo: always "solo"
        - Duo/Team: Team role → Uni role → display_name
        """
        if channel_type is ChannelType.SOLO:
            return "solo"

        name = self._get_role_name(member, "Team ")
        if name is None:
            name = self._get_role_name(member, "Uni ")
        if name is None:
            name = member.display_name

        suffix = " Duo" if channel_type is ChannelType.DUO else " Team"
        return f"{name}{suffix}"

    def _build_overwrites(
        self,
        member: discord.Member,
        channel_type: ChannelType,
    ) -> dict[discord.Role | discord.Member, discord.PermissionOverwrite]:
        """Build permission overwrites for a new dynamic channel.

        Solo/Duo: no restrictions (inherits category permissions).
        Team: deny @everyone connect, allow matching Team role and coach/admin roles.
        """
        if channel_type in (ChannelType.SOLO, ChannelType.DUO):
            return {}  # inherit defaults

        overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {}

        # Deny @everyone connect
        everyone_role = member.guild.default_role
        overwrites[everyone_role] = discord.PermissionOverwrite(connect=False)

        # Find and allow the creator's Team role
        team_role = self._find_team_role(member)
        if team_role is not None:
            overwrites[team_role] = discord.PermissionOverwrite(connect=True)
        else:
            # Fallback: allow creator explicitly when they have no Team role
            overwrites[member] = discord.PermissionOverwrite(connect=True)

        for role in getattr(member.guild, "roles", ()):
            if self._role_is_coach_or_admin(role):
                overwrites[role] = discord.PermissionOverwrite(connect=True)

        return overwrites

    @staticmethod
    def _find_team_role(member: discord.Member) -> Optional[discord.Role]:
        """Return the first role with prefix 'Team ' from the member's roles (case-insensitive)."""
        for role in member.roles:
            if role.name.lower().startswith("team "):
                return role
        return None

    def _next_number(self, guild_id: int, label: str) -> int:
        """Return the lowest unused number for *label* in a guild."""
        used: set[int] = set()
        for info in self._channels.get(guild_id, {}).values():
            if info.label == label:
                used.add(info.number)
        n = 1
        while n in used:
            n += 1
        return n

    @staticmethod
    def _parse_channel_name(name: str) -> Optional[tuple[ChannelType, str, int]]:
        """Parse a dynamic channel name into (type, label, number)."""
        m = _SOLO_RE.match(name)
        if m:
            return ChannelType.SOLO, "solo", int(m.group(1))
        m = _DUO_RE.match(name)
        if m:
            return ChannelType.DUO, f"{m.group(1)} Duo", int(m.group(2))
        m = _TEAM_RE.match(name)
        if m:
            return ChannelType.TEAM, f"{m.group(1)} Team", int(m.group(2))
        return None

    async def _schedule_delete(self, channel: discord.VoiceChannel) -> None:
        """Wait briefly, then delete *channel* if still empty."""
        existing = self._delete_tasks.pop(channel.id, None)
        if existing is not None:
            existing.cancel()
        task = asyncio.create_task(
            self._delayed_delete(channel),
            name=f"dv-delete-{channel.id}",
        )
        self._delete_tasks[channel.id] = task

    async def _delayed_delete(self, channel: discord.VoiceChannel) -> None:
        """Delay, recheck membership, then delete and untrack."""
        try:
            await asyncio.sleep(_DELETE_DELAY_SECONDS)
            async with self._lock:
                fresh = channel.guild.get_channel(channel.id)
                if fresh is None:
                    self._untrack(channel.guild.id, channel.id)
                    return
                if isinstance(fresh, discord.VoiceChannel) and len(fresh.members) > 0:
                    return
                channel_to_delete = fresh
                channel_name = fresh.name
                guild_id = fresh.guild.id
                channel_id = fresh.id
            try:
                await channel_to_delete.delete(reason="Dynamic voice channel empty")
                logger.info("Deleted empty channel %s (id=%s)", channel_name, channel_id)
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.error("Failed to delete channel %s: %s", channel_name, exc)
                return
            async with self._lock:
                self._untrack(guild_id, channel_id)
        except asyncio.CancelledError:
            pass
        finally:
            self._delete_tasks.pop(channel.id, None)

    def _untrack(self, guild_id: int, channel_id: int) -> None:
        guild_channels = self._channels.get(guild_id)
        if guild_channels is not None:
            guild_channels.pop(channel_id, None)
