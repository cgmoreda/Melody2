"""Cog for dynamic voice channel creation and cleanup."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from services.dynamic_voice import ChannelType, DynamicVoiceManager

logger = logging.getLogger(__name__)


class DynamicVoiceCog(commands.Cog, name="DynamicVoice"):
    """Listens to voice state changes and delegates to DynamicVoiceManager."""

    def __init__(self, bot: commands.Bot, manager: DynamicVoiceManager) -> None:
        self.bot = bot
        self._manager = manager

    # ── listeners ───────────────────────────────────────────────

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
                # Enforce access rules (role-based for team, overwrite-based for invite)
                allowed = await self._manager.check_access(member, after.channel)
                if not allowed:
                    try:
                        await member.move_to(None, reason="Not authorized for this channel")
                        logger.info(
                            "Blocked member %s from channel %s (not authorized)",
                            member.id,
                            after.channel.name,
                        )
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        logger.error("Failed to remove unauthorized member %s: %s", member.id, exc)
                else:
                    # Cancel any pending deletion since someone joined
                    self._manager.cancel_pending_delete(after.channel.id)

    # ── commands ─────────────────────────────────────────────────

    @commands.command(name="invite")
    async def invite_cmd(self, ctx: commands.Context, target: discord.Member) -> None:
        """Invite a user to your invite-only voice channel.

        Usage: !invite @user
        Only works when you are inside a tracked invite-only voice channel.
        The invited user will receive a DM and a mention in the text channel.
        """
        # Ensure the author is in a voice channel
        voice_state = ctx.author.voice
        if voice_state is None or voice_state.channel is None:
            await ctx.send(
                "\u274c You must be in a voice channel to use this command.",
                delete_after=10,
            )
            return

        channel = voice_state.channel
        if not isinstance(channel, discord.VoiceChannel):
            await ctx.send(
                "\u274c This command only works in voice channels.",
                delete_after=10,
            )
            return

        # Must be a tracked invite-type channel
        info = self._manager.get_tracked_info(channel.id)
        if info is None or info.channel_type is not ChannelType.INVITE:
            await ctx.send(
                "\u274c This command only works inside an **Invite** voice channel.",
                delete_after=10,
            )
            return

        # Cannot invite bots
        if target.bot:
            await ctx.send("\u274c You cannot invite a bot.", delete_after=10)
            return

        # Cannot invite yourself
        if target.id == ctx.author.id:
            await ctx.send("\u274c You cannot invite yourself.", delete_after=10)
            return

        # Check if target is already in the channel
        if target in channel.members:
            await ctx.send(
                f"\u274c {target.mention} is already in the channel.",
                delete_after=10,
            )
            return

        # Check if target already has permission (already invited)
        if self._manager._has_connect_overwrite(channel, target):
            await ctx.send(
                f"\u2139\ufe0f {target.mention} has already been invited.",
                delete_after=10,
            )
            return

        # Grant access
        success = await self._manager.invite_user(channel, target)
        if not success:
            await ctx.send(
                "\u274c Failed to send the invite. Please try again.",
                delete_after=10,
            )
            return

        # Confirmation in text channel
        await ctx.send(
            f"\u2705 {target.mention} has been invited to **{channel.name}** "
            f"by {ctx.author.mention}! Join the voice channel now."
        )

        # DM the invited user
        try:
            await target.send(
                f"\U0001f4e8 You have been invited to **{channel.name}** "
                f"in **{ctx.guild.name}** by **{ctx.author.display_name}**!\n"
                f"Head over and join the voice channel."
            )
        except (discord.Forbidden, discord.HTTPException):
            # DMs might be disabled; the text-channel mention is enough
            logger.info("Could not DM invite notification to member %s", target.id)


async def setup(bot: commands.Bot) -> None:
    manager = getattr(bot, "dynamic_voice", None)
    if manager is None:
        logger.warning("DynamicVoiceCog not loaded because service is missing")
        return
    await bot.add_cog(DynamicVoiceCog(bot, manager))
    logger.info("DynamicVoiceCog loaded")
