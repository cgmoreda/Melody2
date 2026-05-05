from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands

from services.command_parser import CommandParseError, join_free_text
from services.daily_sheet_reminder import (
    DEFAULT_DAILY_SHEET_MESSAGE,
    DailySheetReminderService,
    format_utc_time,
    parse_utc_time,
)

logger = logging.getLogger(__name__)
DAILY_SHEETS_SET_USAGE = (
    "Usage: **!dailysheets set <HH:MM UTC> [#channel] [message]**\n"
    "or **!dailysheets set [#channel] <HH:MM UTC> [message]**"
)


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
        *args: str,
    ) -> None:
        """Set the daily sheets reminder time, channel, and optional message.

        Time is configured in UTC. Example:
        `!dailysheets set 20:30 #daily-sheets Please update your daily sheets.`
        `!dailysheets set #daily-sheets 20:30 Please update your daily sheets.`
        """
        assert ctx.guild is not None
        try:
            hour, minute, channel, message = await self._parse_set_args(ctx, args)
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
                message=message,
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

    @staticmethod
    def _try_parse_utc_time(raw: str) -> Optional[tuple[int, int]]:
        try:
            return parse_utc_time(raw)
        except ValueError:
            return None

    async def _resolve_text_channel_arg(
        self,
        ctx: commands.Context,
        raw: str,
    ) -> Optional[discord.TextChannel]:
        assert ctx.guild is not None
        token = raw.strip()
        if token.startswith("<#") and token.endswith(">"):
            channel_id_text = token[2:-1]
            if channel_id_text.isdigit():
                channel = ctx.guild.get_channel(int(channel_id_text))
                if isinstance(channel, discord.TextChannel):
                    return channel

        if token.startswith("#"):
            channel_name = token[1:].casefold()
            for channel in getattr(ctx.guild, "text_channels", ()):
                if getattr(channel, "name", "").casefold() == channel_name and isinstance(channel, discord.TextChannel):
                    return channel

        try:
            channel = await commands.TextChannelConverter().convert(ctx, raw)
        except (commands.BadArgument, AttributeError):
            return None
        if isinstance(channel, discord.TextChannel):
            return channel
        return None

    async def _parse_set_args(
        self,
        ctx: commands.Context,
        args: tuple[str, ...],
    ) -> tuple[int, int, Optional[discord.TextChannel], str]:
        if not args:
            raise CommandParseError(DAILY_SHEETS_SET_USAGE)

        first_time = self._try_parse_utc_time(args[0])
        if first_time is not None:
            if len(args) > 1 and self._try_parse_utc_time(args[1]) is not None:
                raise CommandParseError("Only one `time` clause is allowed.")
            channel = await self._resolve_text_channel_arg(ctx, args[1]) if len(args) > 1 else None
            message_start = 2 if channel is not None else 1
            return (
                first_time[0],
                first_time[1],
                channel,
                join_free_text(args[message_start:]) or DEFAULT_DAILY_SHEET_MESSAGE,
            )

        channel = await self._resolve_text_channel_arg(ctx, args[0])
        if channel is None:
            parse_utc_time(args[0])
            raise CommandParseError(DAILY_SHEETS_SET_USAGE)
        if len(args) < 2:
            raise CommandParseError(DAILY_SHEETS_SET_USAGE)

        second_time = self._try_parse_utc_time(args[1])
        if second_time is None:
            parse_utc_time(args[1])
            raise CommandParseError(DAILY_SHEETS_SET_USAGE)
        if len(args) > 2 and self._try_parse_utc_time(args[2]) is not None:
            raise CommandParseError("Only one `time` clause is allowed.")
        return (
            second_time[0],
            second_time[1],
            channel,
            join_free_text(args[2:]) or DEFAULT_DAILY_SHEET_MESSAGE,
        )

    @dailysheets_set.error
    async def dailysheets_set_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You need the **Manage Server** permission to change daily sheets reminders.")
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(DAILY_SHEETS_SET_USAGE)
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
