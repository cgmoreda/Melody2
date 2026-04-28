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

            try:
                new_channel = await member.guild.create_voice_channel(
                    name=channel_name,
                    user_limit=user_limit,
                    category=category,
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
                    creator_id=0,
                    label=label,
                    number=number,
                )
                guild_channels[vc.id] = tracked
                logger.info("Recovered tracked channel %s (id=%s)", vc.name, vc.id)

    # ── internals ───────────────────────────────────────────────

    @staticmethod
    def _get_role_name(member: discord.Member, prefix: str) -> Optional[str]:
        """Return the name portion after *prefix* from the member's roles."""
        for role in member.roles:
            if role.name.startswith(prefix):
                return role.name[len(prefix):]
        return None

    def _build_label(self, member: discord.Member, channel_type: ChannelType) -> str:
        """Build the base label for a channel (without the #N suffix).

        Fallback chain:
        - Solo: always "solo"
        - Duo:  Uni role → display_name
        - Team: Team role → Uni role → display_name
        """
        if channel_type is ChannelType.SOLO:
            return "solo"

        if channel_type is ChannelType.DUO:
            name = self._get_role_name(member, "Uni ")
            if name is None:
                name = member.display_name
            return f"{name} Duo"

        # TEAM: Team role → Uni role → display_name
        name = self._get_role_name(member, "Team ")
        if name is None:
            name = self._get_role_name(member, "Uni ")
        if name is None:
            name = member.display_name
        return f"{name} Team"

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
