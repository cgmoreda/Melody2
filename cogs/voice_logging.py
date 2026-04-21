from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Optional

import discord
from discord.ext import commands

from db.repository import UserRepositoryBase
from services.guild_config import GuildConfigService


def _hours(seconds: float) -> str:
    return f"{seconds / 3600:.2f}h"


def _is_solo_channel(channel: Optional[discord.abc.GuildChannel]) -> bool:
    return channel is not None and "solo" in channel.name.lower()


class WorkConfirmationView(discord.ui.View):
    def __init__(self, member_id: int, timeout_seconds: int) -> None:
        super().__init__(timeout=timeout_seconds)
        self.member_id = member_id
        self.confirmed = False

    @discord.ui.button(label="Yes, still working", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.member_id:
            await interaction.response.send_message("This prompt is not for you.", ephemeral=True)
            return

        self.confirmed = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Confirmed. Keeping your session active.", view=self)
        self.stop()


class VoiceLoggingCog(commands.Cog, name="VoiceLogging"):
    def __init__(self, bot: commands.Bot, repo: UserRepositoryBase, config_service: GuildConfigService) -> None:
        self.bot = bot
        self._repo = repo
        self._config = config_service
        self._watchdogs: dict[int, asyncio.Task[None]] = {}

    def cog_unload(self) -> None:
        for task in self._watchdogs.values():
            task.cancel()
        self._watchdogs.clear()

    async def _start_voice_session(
        self,
        *,
        guild_id: int,
        member_id: int,
        channel: discord.VoiceChannel,
        started_at: datetime,
    ) -> None:
        await self._repo.start_voice_session(
            guild_id=guild_id,
            discord_id=member_id,
            channel_id=channel.id,
            channel_name=channel.name,
            is_solo=_is_solo_channel(channel),
            started_at=started_at,
        )

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        now = datetime.now(tz=UTC)
        for guild in self.bot.guilds:
            for voice_channel in guild.voice_channels:
                if not _is_solo_channel(voice_channel):
                    continue
                for member in voice_channel.members:
                    if member.bot:
                        continue
                    has_open = await self._repo.has_open_voice_session(guild.id, member.id)
                    if not has_open:
                        await self._start_voice_session(
                            guild_id=guild.id,
                            member_id=member.id,
                            channel=voice_channel,
                            started_at=now,
                        )
                    self._start_watchdog(member)

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

        now = datetime.now(tz=UTC)
        await self._repo.close_open_voice_sessions(member.guild.id, member.id, now)
        self._stop_watchdog(member.id)

        if not isinstance(after.channel, discord.VoiceChannel):
            return

        await self._start_voice_session(
            guild_id=member.guild.id,
            member_id=member.id,
            channel=after.channel,
            started_at=now,
        )
        if _is_solo_channel(after.channel):
            self._start_watchdog(member)

    def _start_watchdog(self, member: discord.Member) -> None:
        self._stop_watchdog(member.id)
        self._watchdogs[member.id] = asyncio.create_task(
            self._watchdog_loop(member.guild.id, member.id),
            name=f"solo-watchdog-{member.id}",
        )

    def _stop_watchdog(self, member_id: int) -> None:
        task = self._watchdogs.pop(member_id, None)
        if task is not None:
            task.cancel()

    async def _watchdog_loop(self, guild_id: int, member_id: int) -> None:
        try:
            while True:
                config = await self._config.get(guild_id)
                await asyncio.sleep(config.voice_check_interval_seconds)

                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    break

                member = guild.get_member(member_id)
                if member is None:
                    break

                voice_channel = member.voice.channel if member.voice else None
                if not _is_solo_channel(voice_channel):
                    break

                confirmed = await self._ask_still_working(member, config.voice_confirm_timeout_seconds)
                if confirmed is True or confirmed is None:
                    continue

                disconnected = False
                if member.voice is not None:
                    try:
                        await member.move_to(None, reason="No response to solo-channel work check")
                        disconnected = True
                    except (discord.Forbidden, discord.HTTPException):
                        disconnected = False

                if not disconnected:
                    try:
                        await member.send(
                            "You did not confirm in time. I could not disconnect you due to missing permissions."
                        )
                    except discord.Forbidden:
                        pass
                    continue

                await self._repo.close_open_voice_sessions(guild.id, member.id, datetime.now(tz=UTC))
                try:
                    await member.send("You were disconnected because you did not confirm in time.")
                except discord.Forbidden:
                    pass
                break
        except asyncio.CancelledError:
            pass
        finally:
            self._watchdogs.pop(member_id, None)

    async def _ask_still_working(self, member: discord.Member, timeout_seconds: int) -> Optional[bool]:
        view = WorkConfirmationView(member.id, timeout_seconds)
        timeout_minutes = max(1, timeout_seconds // 60)
        prompt = (
            "Are you still working in the solo channel?\n"
            f"Click **Yes, still working** within {timeout_minutes} minutes to keep your session."
        )
        try:
            message = await member.send(prompt, view=view)
        except discord.Forbidden:
            return None

        await view.wait()
        if view.confirmed:
            return True

        for child in view.children:
            child.disabled = True
        try:
            await message.edit(content="No response received in time.", view=view)
        except discord.HTTPException:
            pass
        return False

    @commands.command(name="voicehours", aliases=["solohours"])
    @commands.guild_only()
    async def voicehours(self, ctx: commands.Context) -> None:
        assert ctx.guild is not None

        config = await self._config.get(ctx.guild.id)
        now = datetime.now(tz=UTC)
        week_since = now - timedelta(days=7)
        month_since = now - timedelta(days=30)

        week = await self._repo.get_solo_voice_totals(ctx.guild.id, now=now, since=week_since)
        month = await self._repo.get_solo_voice_totals(ctx.guild.id, now=now, since=month_since)
        all_time = await self._repo.get_solo_voice_totals(ctx.guild.id, now=now, since=None)

        if not all_time and not week and not month:
            await ctx.send("No solo-channel voice logs found yet.")
            return

        verified = await self._repo.get_all(ctx.guild.id)
        handle_by_discord_id = {row.discord_id: row.cf_handle for row in verified}

        all_ids = set(week) | set(month) | set(all_time)
        ordered_ids = sorted(all_ids, key=lambda user_id: all_time.get(user_id, 0.0), reverse=True)

        lines: list[str] = []
        for discord_id in ordered_ids:
            handle = handle_by_discord_id.get(discord_id)
            if handle is None:
                member = ctx.guild.get_member(discord_id)
                handle = member.display_name if member else str(discord_id)
            lines.append(
                f"`{handle}` | {_hours(week.get(discord_id, 0.0))} | "
                f"{_hours(month.get(discord_id, 0.0))} | {_hours(all_time.get(discord_id, 0.0))}"
            )

        header = "**Handle | Last Week | Last Month | All Time**\n"
        body = "\n".join(lines[: config.voicehours_max_lines])
        extra = len(lines) - min(len(lines), config.voicehours_max_lines)
        if extra > 0:
            body += f"\n... and {extra} more users"
        await ctx.send(header + body)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VoiceLoggingCog(bot, getattr(bot, "user_repo"), getattr(bot, "guild_config")))
