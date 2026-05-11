"""Coach secretary cog for guild configuration and voice-state routing."""

from __future__ import annotations

import logging
from typing import Union

import discord
from discord.ext import commands

from db.repository import CoachConfig
from services.coach_secretary import CoachSecretaryBase

logger = logging.getLogger(__name__)
SummonTarget = Union[discord.Member, discord.Role]


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
        """Manage coach secretary routing settings."""
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
        """Remove coach secretary configuration for this server."""
        assert ctx.guild is not None

        removed = await self._secretary.remove_config(ctx.guild.id)
        if removed:
            await ctx.send("Coach secretary configuration removed.")
            return
        await ctx.send("No coach configuration exists for this server.")

    @coach.command(name="config")
    @commands.guild_only()
    async def coach_config(self, ctx: commands.Context) -> None:
        """Show the current coach secretary configuration."""
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

    @commands.command(name="summon")
    @commands.guild_only()
    async def summon(self, ctx: commands.Context, target: SummonTarget) -> None:
        """Summon a member or role to the configured coach room."""
        assert ctx.guild is not None

        config = await self._secretary.get_config(ctx.guild.id)
        if config is None:
            await ctx.send("Coach secretary is not configured. Use !coach setup first.")
            return
        if ctx.author.id != config.coach_id:
            await ctx.send("Only the configured coach can use this command.")
            return

        coach_room = ctx.guild.get_channel(config.coach_channel_id)
        if not isinstance(coach_room, discord.VoiceChannel):
            await ctx.send("Coach room channel was not found. Use !coach setup again.")
            return

        members = self._members_from_summon_target(target)
        if not members:
            await ctx.send("No non-bot members found for that target.")
            return

        result = await self._summon_members(
            guild=ctx.guild,
            coach=ctx.author,
            coach_room=coach_room,
            members=members,
        )
        await ctx.send(self._render_summon_summary(coach_room, result))

    @staticmethod
    def _members_from_summon_target(target: SummonTarget) -> list[discord.Member]:
        if isinstance(target, discord.Member):
            return [] if target.bot else [target]
        if isinstance(target, discord.Role):
            return [member for member in target.members if not member.bot]
        return []

    async def _summon_members(
        self,
        *,
        guild: discord.Guild,
        coach: discord.Member,
        coach_room: discord.VoiceChannel,
        members: list[discord.Member],
    ) -> dict[str, int]:
        result = {
            "moved": 0,
            "already": 0,
            "dm_sent": 0,
            "dm_failed": 0,
            "move_failed": 0,
        }
        reason = f"Summoned by coach {coach} ({coach.id})"

        for member in members:
            voice_channel = member.voice.channel if member.voice is not None else None
            if voice_channel is None:
                try:
                    await member.send(
                        f"{coach.display_name} asked you to join the coach's office in "
                        f"{guild.name}: {coach_room.name}."
                    )
                    result["dm_sent"] += 1
                except (discord.Forbidden, discord.HTTPException):
                    result["dm_failed"] += 1
                continue

            if voice_channel.id == coach_room.id:
                result["already"] += 1
                continue

            try:
                await member.move_to(coach_room, reason=reason)
                result["moved"] += 1
            except (discord.Forbidden, discord.HTTPException):
                result["move_failed"] += 1

        return result

    @staticmethod
    def _render_summon_summary(coach_room: discord.VoiceChannel, result: dict[str, int]) -> str:
        parts = [f"Summon to {coach_room.mention} complete."]
        if result["moved"]:
            parts.append(f"Moved: **{result['moved']}**.")
        if result["already"]:
            parts.append(f"Already there: **{result['already']}**.")
        if result["dm_sent"]:
            parts.append(f"DM'd: **{result['dm_sent']}**.")
        if result["move_failed"]:
            parts.append(f"Move failed: **{result['move_failed']}**.")
        if result["dm_failed"]:
            parts.append(f"DM failed: **{result['dm_failed']}**.")
        return " ".join(parts)

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

    @summon.error
    async def summon_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Usage: !summon <@user|@role>")
            return
        if isinstance(error, commands.BadUnionArgument):
            await ctx.send("Could not resolve that target. Mention a valid user or role.")
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
