"""Coach Secretary cog — listens to voice state updates and provides coach commands.

Commands
--------
!coach setup @user "WaitingRoom" "CoachRoom"  — Configure coach for this server
!coach reset                                   — Remove coach configuration
!coach config                                  — Show current configuration
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from db.repository import CoachConfig
from services.coach_secretary import CoachSecretaryBase

logger = logging.getLogger(__name__)


class CoachSecretaryCog(commands.Cog, name="Coach Secretary"):
    """Voice-channel routing cog with approval flow.

    When a member joins the waiting room, the coach receives a DM
    with Accept/Reject buttons. The member is only moved on approval.
    """

    def __init__(self, bot: commands.Bot, secretary: CoachSecretaryBase) -> None:
        self.bot = bot
        self._secretary = secretary

    # ── voice state listener ───────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return

        # Only trigger when a user *joins* a channel.
        if after.channel is None:
            return
        if before.channel is not None and before.channel.id == after.channel.id:
            return

        # Check if this channel is a configured waiting room.
        config = await self._secretary.get_config(member.guild.id)
        if config is None or after.channel.id != config.waiting_room_id:
            return

        logger.info("%s joined the waiting room in guild %s", member, member.guild.id)
        await self._secretary.handle_waiting_member(member, member.guild)

    # ── coach command group ────────────────────────────────────

    @commands.group(name="coach", invoke_without_command=True)
    @commands.guild_only()
    async def coach(self, ctx: commands.Context) -> None:
        """Coach management commands."""
        await ctx.send_help(ctx.command)

    # ── !coach setup ───────────────────────────────────────────

    @coach.command(name="setup")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def coach_setup(
        self,
        ctx: commands.Context,
        coach_user: discord.Member,
        waiting_room_name: str,
        coach_room_name: str,
    ) -> None:
        """Configure the coach secretary for this server.

        Usage: !coach setup @CoachUser "Waiting Room" "Coach Room"
        """
        assert ctx.guild is not None

        # Find voice channels by name (case-insensitive).
        waiting_room = discord.utils.find(
            lambda c: isinstance(c, discord.VoiceChannel) and c.name.lower() == waiting_room_name.lower(),
            ctx.guild.channels,
        )
        coach_room = discord.utils.find(
            lambda c: isinstance(c, discord.VoiceChannel) and c.name.lower() == coach_room_name.lower(),
            ctx.guild.channels,
        )

        if waiting_room is None:
            await ctx.send(f"❌ Voice channel **{waiting_room_name}** not found.")
            return
        if coach_room is None:
            await ctx.send(f"❌ Voice channel **{coach_room_name}** not found.")
            return

        config = CoachConfig(
            guild_id=ctx.guild.id,
            coach_id=coach_user.id,
            waiting_room_id=waiting_room.id,
            coach_channel_id=coach_room.id,
        )
        await self._secretary.save_config(config)

        embed = discord.Embed(
            title="✅ Coach Secretary Configured",
            description=(
                f"**Coach:** {coach_user.mention}\n"
                f"**Waiting Room:** {waiting_room.mention}\n"
                f"**Coach Room:** {coach_room.mention}"
            ),
            colour=discord.Colour.green(),
        )
        await ctx.send(embed=embed)

    @coach_setup.error
    async def coach_setup_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(
                '**Usage:**\n'
                '`!coach setup @CoachUser "Waiting Room" "Coach Room"`'
            )
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send("❌ Could not find that user. Make sure to mention them correctly.")
        elif isinstance(error, commands.MissingPermissions):
            await ctx.send("⛔ You need **Administrator** permission to configure the coach.")
        else:
            raise error

    # ── !coach reset ───────────────────────────────────────────

    @coach.command(name="reset")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def coach_reset(self, ctx: commands.Context) -> None:
        """Remove the coach secretary configuration for this server."""
        assert ctx.guild is not None
        removed = await self._secretary.remove_config(ctx.guild.id)
        if removed:
            await ctx.send("🗑️ Coach secretary configuration removed.")
        else:
            await ctx.send("⚠️ No coach configuration found for this server.")

    # ── !coach config ──────────────────────────────────────────

    @coach.command(name="config")
    @commands.guild_only()
    async def coach_config(self, ctx: commands.Context) -> None:
        """Show the current coach secretary configuration."""
        assert ctx.guild is not None
        config = await self._secretary.get_config(ctx.guild.id)
        if config is None:
            await ctx.send("⚠️ Coach secretary is not configured. Use `!coach setup` first.")
            return

        coach = ctx.guild.get_member(config.coach_id)
        waiting = ctx.guild.get_channel(config.waiting_room_id)
        office = ctx.guild.get_channel(config.coach_channel_id)

        embed = discord.Embed(
            title="⚙️ Coach Secretary Config",
            description=(
                f"**Coach:** {coach.mention if coach else 'Unknown'}\n"
                f"**Waiting Room:** {waiting.mention if waiting else 'Unknown'}\n"
                f"**Coach Room:** {office.mention if office else 'Unknown'}"
            ),
            colour=discord.Colour.blurple(),
        )
        await ctx.send(embed=embed)

    # ── error handlers ─────────────────────────────────────────

    @coach_reset.error
    async def coach_permission_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("⛔ You don't have permission to do that.")
        else:
            raise error


# ── Extension entry-point ──────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    """Called by ``bot.load_extension("cogs.coach_secretary")``."""
    secretary = getattr(bot, "coach_secretary", None)
    if secretary is None:
        logger.warning("Coach Secretary cog skipped — service not configured")
        return
    await bot.add_cog(CoachSecretaryCog(bot, secretary))
    logger.info("Coach Secretary cog loaded")
