from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands

from services.daily_sheet_reminder import (
    DEFAULT_DAILY_SHEET_MESSAGE,
    DailySheetReminderService,
    format_utc_time,
    parse_utc_time,
)

logger = logging.getLogger(__name__)


class DailySheetsCog(commands.Cog, name="DailySheets"):
    def __init__(self, reminder: DailySheetReminderService) -> None:
        self._reminder = reminder

    @commands.group(name="dailysheets", aliases=["dailyreminder", "sheets"], invoke_without_command=True)
    @commands.guild_only()
    async def dailysheets(self, ctx: commands.Context) -> None:
        """Manage daily sheets reminders."""
        await ctx.send("Usage: **!dailysheets <set|status|disable>**")

    @dailysheets.command(name="set")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def dailysheets_set(
        self,
        ctx: commands.Context,
        time_utc: str,
        channel: Optional[discord.TextChannel] = None,
        *,
        message: Optional[str] = None,
    ) -> None:
        """Set the daily sheets reminder time, channel, and optional message.

        Time is configured in UTC. Example:
        `!dailysheets set 20:30 #daily-sheets Please update your daily sheets.`
        """
        assert ctx.guild is not None
        try:
            hour, minute = parse_utc_time(time_utc)
        except ValueError as exc:
            await ctx.send(str(exc))
            return

        target = channel or ctx.channel
        if not isinstance(target, discord.TextChannel):
            await ctx.send("This command must target a text channel.")
            return

        try:
            config = await self._reminder.set_reminder(
                guild_id=ctx.guild.id,
                channel_id=target.id,
                remind_hour_utc=hour,
                remind_minute_utc=minute,
                message=message or DEFAULT_DAILY_SHEET_MESSAGE,
            )
        except ValueError as exc:
            await ctx.send(str(exc))
            return

        await ctx.send(
            f"Daily sheets reminder set in {target.mention} at "
            f"`{format_utc_time(config.remind_hour_utc, config.remind_minute_utc)}`."
        )

    @dailysheets.command(name="status")
    @commands.guild_only()
    async def dailysheets_status(self, ctx: commands.Context) -> None:
        """Show the configured daily sheets reminder."""
        assert ctx.guild is not None
        config = await self._reminder.get_reminder(ctx.guild.id)
        if config is None:
            await ctx.send("Daily sheets reminder is not configured.")
            return

        channel = ctx.guild.get_channel(config.channel_id)
        channel_text = channel.mention if isinstance(channel, discord.TextChannel) else f"`{config.channel_id}`"
        sent_text = config.last_sent_on.isoformat() if config.last_sent_on is not None else "never"
        embed = discord.Embed(
            title="Daily Sheets Reminder",
            colour=discord.Colour.blurple(),
        )
        embed.add_field(name="Channel", value=channel_text, inline=True)
        embed.add_field(
            name="Time",
            value=format_utc_time(config.remind_hour_utc, config.remind_minute_utc),
            inline=True,
        )
        embed.add_field(name="Last sent", value=sent_text, inline=True)
        embed.add_field(name="Message", value=config.message, inline=False)
        await ctx.send(embed=embed)

    @dailysheets.command(name="disable")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def dailysheets_disable(self, ctx: commands.Context) -> None:
        """Disable the daily sheets reminder for this server."""
        assert ctx.guild is not None
        removed = await self._reminder.disable_reminder(ctx.guild.id)
        if removed:
            await ctx.send("Daily sheets reminder disabled.")
            return
        await ctx.send("Daily sheets reminder was not configured.")

    @dailysheets_set.error
    async def dailysheets_set_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You need the **Manage Server** permission to change daily sheets reminders.")
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Usage: **!dailysheets set <HH:MM UTC> [#channel] [message]**")
            return
        raise error

    @dailysheets_disable.error
    async def dailysheets_disable_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You need the **Manage Server** permission to disable daily sheets reminders.")
            return
        raise error


async def setup(bot: commands.Bot) -> None:
    reminder = getattr(bot, "daily_sheet_reminder", None)
    if reminder is None:
        logger.warning("DailySheetsCog not loaded because service is missing")
        return
    await bot.add_cog(DailySheetsCog(reminder))
