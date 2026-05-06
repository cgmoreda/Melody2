"""Cog for dynamic voice channel creation and cleanup."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from services.dynamic_voice import DynamicVoiceManager

logger = logging.getLogger(__name__)


class DynamicVoiceCog(commands.Cog, name="DynamicVoice"):
    """Listens to voice state changes and delegates to DynamicVoiceManager."""

    def __init__(self, bot: commands.Bot, manager: DynamicVoiceManager) -> None:
        self.bot = bot
        self._manager = manager

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            await self._manager.rebuild_state(guild)
        logger.info("Dynamic voice state rebuilt for %d guild(s)", len(self.bot.guilds))

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return
        if before.channel is after.channel:
            return

        # Handle leave from a tracked dynamic channel
        if before.channel is not None and isinstance(before.channel, discord.VoiceChannel):
            await self._manager.handle_leave(before.channel)

        # Handle join to an entry channel OR a tracked dynamic channel
        if after.channel is not None and isinstance(after.channel, discord.VoiceChannel):
            channel_type = self._manager.is_entry_channel(after.channel.name)
            if channel_type is not None:
                await self._manager.handle_join(member, after.channel, channel_type)
            elif self._manager.is_tracked(after.channel.id):
                # Enforce role-based access for team channels
                allowed = await self._manager.check_access(member, after.channel)
                if not allowed:
                    try:
                        await member.move_to(None, reason="No matching team role for this channel")
                        logger.info(
                            "Blocked member %s from channel %s (no matching role)",
                            member.id,
                            after.channel.name,
                        )
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        logger.error("Failed to remove unauthorized member %s: %s", member.id, exc)
                else:
                    # Cancel any pending deletion since someone joined
                    self._manager.cancel_pending_delete(after.channel.id)


async def setup(bot: commands.Bot) -> None:
    manager = getattr(bot, "dynamic_voice", None)
    if manager is None:
        logger.warning("DynamicVoiceCog not loaded because service is missing")
        return
    await bot.add_cog(DynamicVoiceCog(bot, manager))
    logger.info("DynamicVoiceCog loaded")
