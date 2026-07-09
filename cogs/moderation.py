import logging
from typing import Optional

import discord
from discord.ext import commands

from services.command_parser import CommandParseError, parse_timeout_duration

logger = logging.getLogger(__name__)


class ModerationCog(commands.Cog, name="Moderation"):
    """Moderation commands for managing the server."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="timeout")
    @commands.has_permissions(moderate_members=True)
    async def timeout_command(
        self,
        ctx: commands.Context,
        member: discord.Member,
        duration_raw: str,
        unit_raw: Optional[str] = None,
    ) -> None:
        """Time out a member for a specified duration."""
        if ctx.guild is None or ctx.author is None:
            return

        if member == ctx.author:
            await ctx.send("You cannot time yourself out.")
            return

        if member == self.bot.user:
            await ctx.send("I cannot time myself out.")
            return

        if member.id == ctx.guild.owner_id:
            await ctx.send("You cannot time out the server owner.")
            return

        if ctx.author.id != ctx.guild.owner_id:
            if isinstance(ctx.author, discord.Member):
                if member.top_role >= ctx.author.top_role:
                    await ctx.send("You cannot time out a member with an equal or higher top role.")
                    return

        bot_member = ctx.guild.me
        if bot_member is None:
            await ctx.send("I cannot determine my role in this server.")
            return

        if bot_member.top_role <= member.top_role:
            await ctx.send("I cannot time out a member with an equal or higher top role.")
            return

        try:
            parsed_timedelta, human_readable = parse_timeout_duration(duration_raw, unit_raw)
        except CommandParseError as exc:
            await ctx.send(str(exc))
            return

        reason = f"Timed out by {ctx.author} via !timeout."
        try:
            await member.timeout(parsed_timedelta, reason=reason)
            logger.info("Timed out %s in %s for %s. Reason: %s", member.id, ctx.guild.id, human_readable, reason)
            await ctx.send(f"✅ {member.mention} has been timed out for {human_readable}.")
        except discord.Forbidden:
            await ctx.send("I do not have permission to time out this member.")
        except discord.HTTPException as exc:
            logger.exception("Failed to time out %s in %s", member.id, ctx.guild.id)
            await ctx.send(f"Failed to time out the member: {exc}")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ModerationCog(bot))
