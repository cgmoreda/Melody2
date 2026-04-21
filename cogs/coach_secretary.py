"""Coach secretary cog for guild configuration and voice-state routing."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from db.repository import CoachConfig
from services.coach_secretary import CoachSecretaryBase

logger = logging.getLogger(__name__)


class CoachSecretaryCog(commands.Cog, name="CoachSecretary"):
    """Routes waiting-room members to a coach after manual approval."""

    def __init__(self, bot: commands.Bot, secretary: CoachSecretaryBase) -> None:
        self.bot = bot
        self._secretary = secretary

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return

        config = await self._secretary.get_config(member.guild.id)
        if config is None:
            return

        before_id = before.channel.id if before.channel is not None else None
        after_id = after.channel.id if after.channel is not None else None

        if before_id == config.waiting_room_id and after_id != config.waiting_room_id:
            self._secretary.clear_notification(member.guild.id, member.id)

        joined_waiting_room = after_id == config.waiting_room_id and before_id != config.waiting_room_id
        if not joined_waiting_room:
            return

        logger.info("Member %s joined waiting room in guild %s", member.id, member.guild.id)
        await self._secretary.handle_waiting_member(member, member.guild)

    @commands.group(name="coach", invoke_without_command=True)
    @commands.guild_only()
    async def coach(self, ctx: commands.Context) -> None:
        await ctx.send("Usage: !coach <setup|reset|config>")

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
        """Configure coach user and the two voice channels by name."""
        assert ctx.guild is not None

        waiting_room = discord.utils.find(
            lambda c: isinstance(c, discord.VoiceChannel) and c.name.lower() == waiting_room_name.lower(),
            ctx.guild.channels,
        )
        coach_room = discord.utils.find(
            lambda c: isinstance(c, discord.VoiceChannel) and c.name.lower() == coach_room_name.lower(),
            ctx.guild.channels,
        )

        if waiting_room is None:
            await ctx.send(f"Voice channel '{waiting_room_name}' was not found.")
            return
        if coach_room is None:
            await ctx.send(f"Voice channel '{coach_room_name}' was not found.")
            return

        config = CoachConfig(
            guild_id=ctx.guild.id,
            coach_id=coach_user.id,
            waiting_room_id=waiting_room.id,
            coach_channel_id=coach_room.id,
        )
        await self._secretary.save_config(config)

        embed = discord.Embed(
            title="Coach Secretary Configured",
            description=(
                f"Coach: {coach_user.mention}\n"
                f"Waiting Room: {waiting_room.mention}\n"
                f"Coach Room: {coach_room.mention}"
            ),
            colour=discord.Colour.green(),
        )
        await ctx.send(embed=embed)

    @coach.command(name="reset")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def coach_reset(self, ctx: commands.Context) -> None:
        assert ctx.guild is not None

        removed = await self._secretary.remove_config(ctx.guild.id)
        if removed:
            await ctx.send("Coach secretary configuration removed.")
            return
        await ctx.send("No coach configuration exists for this server.")

    @coach.command(name="config")
    @commands.guild_only()
    async def coach_config(self, ctx: commands.Context) -> None:
        assert ctx.guild is not None

        config = await self._secretary.get_config(ctx.guild.id)
        if config is None:
            await ctx.send("Coach secretary is not configured. Use !coach setup first.")
            return

        coach = ctx.guild.get_member(config.coach_id)
        waiting_room = ctx.guild.get_channel(config.waiting_room_id)
        coach_room = ctx.guild.get_channel(config.coach_channel_id)

        embed = discord.Embed(
            title="Coach Secretary Config",
            description=(
                f"Coach: {coach.mention if coach else 'Missing'}\n"
                f"Waiting Room: {waiting_room.mention if waiting_room else 'Missing'}\n"
                f"Coach Room: {coach_room.mention if coach_room else 'Missing'}"
            ),
            colour=discord.Colour.blurple(),
        )
        await ctx.send(embed=embed)

    @coach_setup.error
    async def coach_setup_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send('Usage: !coach setup @CoachUser "Waiting Room" "Coach Room"')
            return
        if isinstance(error, commands.MemberNotFound):
            await ctx.send("Coach user was not found. Mention a valid member.")
            return
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You need Administrator permission for this command.")
            return
        raise error

    @coach_reset.error
    async def coach_reset_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You need Administrator permission for this command.")
            return
        raise error


async def setup(bot: commands.Bot) -> None:
    secretary = getattr(bot, "coach_secretary", None)
    if secretary is None:
        logger.warning("CoachSecretaryCog not loaded because service is missing")
        return
    await bot.add_cog(CoachSecretaryCog(bot, secretary))
    logger.info("CoachSecretaryCog loaded")