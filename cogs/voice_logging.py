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


def _rank_prefix(rank: int) -> str:
    if rank == 1:
        return "🥇"
    if rank == 2:
        return "🥈"
    if rank == 3:
        return "🥉"
    return f"#{rank}"


def _normalize_window_unit(raw: str) -> Optional[str]:
    unit = raw.strip().lower()
    mapping = {
        "h": "hour",
        "hr": "hour",
        "hrs": "hour",
        "hour": "hour",
        "hours": "hour",
        "d": "day",
        "day": "day",
        "days": "day",
        "w": "week",
        "wk": "week",
        "wks": "week",
        "week": "week",
        "weeks": "week",
        "m": "month",
        "mo": "month",
        "month": "month",
        "months": "month",
    }
    return mapping.get(unit)


def _window_delta(amount: int, unit: str) -> timedelta:
    if unit == "hour":
        return timedelta(hours=amount)
    if unit == "day":
        return timedelta(days=amount)
    if unit == "week":
        return timedelta(weeks=amount)
    if unit == "month":
        return timedelta(days=30 * amount)
    raise ValueError(f"unsupported window unit: {unit}")


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

    async def _resolve_handles(self, guild: discord.Guild) -> dict[int, str]:
        verified = await self._repo.get_all(guild.id)
        handle_by_discord_id = {row.discord_id: row.cf_handle for row in verified}
        return handle_by_discord_id

    def _parse_window_tokens(
        self,
        *,
        now: datetime,
        tokens: tuple[str, ...],
    ) -> tuple[Optional[datetime], str]:
        if not tokens:
            return None, "all time"

        if len(tokens) == 1:
            short = tokens[0].strip().lower()
            if short in {"all", "alltime", "all-time"}:
                return None, "all time"
            normalized = _normalize_window_unit(short)
            if normalized is not None:
                return now - _window_delta(1, normalized), f"last 1 {normalized}"
            raise ValueError("Usage: `... [last <x> <hour/day/week/month>]`")

        if len(tokens) == 3 and tokens[0].strip().lower() == "last":
            try:
                amount = int(tokens[1])
            except ValueError as exc:
                raise ValueError("`x` must be a positive integer.") from exc
            if amount <= 0:
                raise ValueError("`x` must be greater than 0.")

            normalized_unit = _normalize_window_unit(tokens[2])
            if normalized_unit is None:
                raise ValueError("Unit must be one of: `hour`, `day`, `week`, `month`.")
            return (
                now - _window_delta(amount, normalized_unit),
                f"last {amount} {normalized_unit}{'' if amount == 1 else 's'}",
            )

        raise ValueError("Usage: `... [last <x> <hour/day/week/month>]`")

    @staticmethod
    def _sorted_totals(totals: dict[int, float]) -> list[tuple[int, float]]:
        return sorted(totals.items(), key=lambda item: item[1], reverse=True)

    async def _leaderboard_lines(
        self,
        *,
        guild: discord.Guild,
        totals: dict[int, float],
        handle_by_discord_id: dict[int, str],
    ) -> list[str]:
        lines: list[str] = ["rk   handle             hours"]
        for index, (discord_id, seconds) in enumerate(self._sorted_totals(totals), start=1):
            handle = handle_by_discord_id.get(discord_id)
            if handle is None:
                member = guild.get_member(discord_id)
                handle = member.display_name if member else str(discord_id)
            lines.append(f"{_rank_prefix(index):<4} {handle:<18.18} {_hours(seconds)}")
        return lines

    @staticmethod
    def _find_rank(totals: dict[int, float], discord_id: int) -> Optional[tuple[int, float]]:
        if discord_id not in totals:
            return None
        ordered = sorted(totals.items(), key=lambda item: item[1], reverse=True)
        for rank, (candidate_id, seconds) in enumerate(ordered, start=1):
            if candidate_id == discord_id:
                return rank, seconds
        return None

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
    async def voicehours(self, ctx: commands.Context, *args: str) -> None:
        """Show solo voice-channel hours.

        Usage:
        - `!voicehours`
        - `!voicehours last <x> <hour/day/week/month>`
        - `!voicehours me [last <x> <hour/day/week/month>]`
        - `!voicehours user <@member> [last <x> <hour/day/week/month>]`
        - `!voicehours role <@role> [last <x> <hour/day/week/month>]`
        - `!voicehours roles [last <x> <hour/day/week/month>]`
        - `!voicehours top [limit] [last <x> <hour/day/week/month>]`
        """
        assert ctx.guild is not None

        config = await self._config.get(ctx.guild.id)
        now = datetime.now(tz=UTC)
        handle_by_discord_id = await self._resolve_handles(ctx.guild)

        if args:
            mode = args[0].lower()

            if mode == "top":
                limit = config.voicehours_max_lines
                remaining = args[1:]
                if remaining and remaining[0].isdigit():
                    limit = max(1, min(int(remaining[0]), 100))
                    remaining = remaining[1:]

                try:
                    since, label = self._parse_window_tokens(now=now, tokens=tuple(remaining))
                except ValueError as err:
                    await ctx.send(str(err))
                    return

                totals = await self._repo.get_solo_voice_totals(ctx.guild.id, now=now, since=since)
                if not totals:
                    await ctx.send("No solo-channel voice logs found for that period.")
                    return

                lines = await self._leaderboard_lines(guild=ctx.guild, totals=totals, handle_by_discord_id=handle_by_discord_id)
                shown = lines[: limit + 1]
                content = [f"**Top Solo Voice Hours ({label})**", "```text", *shown, "```"]
                rank_data = self._find_rank(totals, ctx.author.id)
                if rank_data is not None:
                    rank, seconds = rank_data
                    content.append(f"Your rank: **#{rank}** with **{_hours(seconds)}**")
                remaining_count = max(0, len(lines) - len(shown))
                if remaining_count > 0:
                    content.append(f"... and {remaining_count} more users")
                await ctx.send("\n".join(content))
                return

            if mode == "me":
                try:
                    since, label = self._parse_window_tokens(now=now, tokens=tuple(args[1:]))
                except ValueError as err:
                    await ctx.send(str(err))
                    return

                totals = await self._repo.get_solo_voice_totals(ctx.guild.id, now=now, since=since)
                rank_data = self._find_rank(totals, ctx.author.id)
                if rank_data is None:
                    await ctx.send(f"You have no solo voice logs for {label}.")
                    return
                rank, seconds = rank_data
                await ctx.send(f"**{ctx.author.display_name}** in {label}: **{_hours(seconds)}** (rank **#{rank}**)")
                return

            if mode == "user":
                if len(args) < 2:
                    await ctx.send("Usage: `!voicehours user <@member> [last <x> <hour/day/week/month>]`")
                    return
                try:
                    target_member = await commands.MemberConverter().convert(ctx, args[1])
                except commands.BadArgument:
                    await ctx.send("Could not resolve that member.")
                    return
                try:
                    since, label = self._parse_window_tokens(now=now, tokens=tuple(args[2:]))
                except ValueError as err:
                    await ctx.send(str(err))
                    return

                totals = await self._repo.get_solo_voice_totals(ctx.guild.id, now=now, since=since)
                seconds = totals.get(target_member.id, 0.0)
                rank_data = self._find_rank(totals, target_member.id)
                rank_text = f" (rank **#{rank_data[0]}**)" if rank_data is not None else ""
                await ctx.send(f"**{target_member.display_name}** in {label}: **{_hours(seconds)}**{rank_text}")
                return

            if mode == "role":
                if len(args) < 2:
                    await ctx.send("Usage: `!voicehours role <@role> [last <x> <hour/day/week/month>]`")
                    return
                try:
                    target_role = await commands.RoleConverter().convert(ctx, args[1])
                except commands.BadArgument:
                    await ctx.send("Could not resolve that role.")
                    return
                try:
                    since, label = self._parse_window_tokens(now=now, tokens=tuple(args[2:]))
                except ValueError as err:
                    await ctx.send(str(err))
                    return

                all_totals = await self._repo.get_solo_voice_totals(ctx.guild.id, now=now, since=since)
                member_ids = {member.id for member in target_role.members if not member.bot}
                totals = {discord_id: value for discord_id, value in all_totals.items() if discord_id in member_ids}
                if not totals:
                    await ctx.send(f"No solo voice logs found for role {target_role.mention} in {label}.")
                    return

                lines = await self._leaderboard_lines(guild=ctx.guild, totals=totals, handle_by_discord_id=handle_by_discord_id)
                shown = lines[: config.voicehours_max_lines + 1]
                content = [f"**Solo Voice Hours for {target_role.name} ({label})**", "```text", *shown, "```"]
                remaining_count = max(0, len(lines) - len(shown))
                if remaining_count > 0:
                    content.append(f"... and {remaining_count} more users")
                await ctx.send("\n".join(content))
                return

            if mode in {"roles", "teams"}:
                try:
                    since, label = self._parse_window_tokens(now=now, tokens=tuple(args[1:]))
                except ValueError as err:
                    await ctx.send(str(err))
                    return

                totals = await self._repo.get_solo_voice_totals(ctx.guild.id, now=now, since=since)
                team_roles = [
                    role
                    for role in ctx.guild.roles
                    if "team" in role.name.lower() and role.name != "@everyone"
                ]
                role_totals: list[tuple[str, float]] = []
                for role in team_roles:
                    seconds_sum = 0.0
                    for member in role.members:
                        if member.bot:
                            continue
                        seconds_sum += totals.get(member.id, 0.0)
                    role_totals.append((role.name, seconds_sum))

                role_totals = [item for item in role_totals if item[1] > 0]
                role_totals.sort(key=lambda item: item[1], reverse=True)
                if not role_totals:
                    await ctx.send(f"No team-role solo voice logs found for {label}.")
                    return

                lines = ["rk   role               total"]
                for index, (role_name, seconds) in enumerate(role_totals, start=1):
                    lines.append(f"{_rank_prefix(index):<4} {role_name:<18.18} {_hours(seconds)}")

                shown = lines[: config.voicehours_max_lines + 1]
                content = [f"**Team Role Solo Voice Standings ({label})**", "```text", *shown, "```"]
                remaining_count = max(0, len(lines) - len(shown))
                if remaining_count > 0:
                    content.append(f"... and {remaining_count} more roles")
                await ctx.send("\n".join(content))
                return

            if mode == "last":
                try:
                    since, label = self._parse_window_tokens(now=now, tokens=tuple(args))
                except ValueError as err:
                    await ctx.send(str(err))
                    return
                totals = await self._repo.get_solo_voice_totals(ctx.guild.id, now=now, since=since)
                if not totals:
                    await ctx.send("No solo-channel voice logs found for that period.")
                    return
                lines = await self._leaderboard_lines(guild=ctx.guild, totals=totals, handle_by_discord_id=handle_by_discord_id)
                shown = lines[: config.voicehours_max_lines + 1]
                content = [f"**Solo Voice Hours ({label})**", "```text", *shown, "```"]
                remaining_count = max(0, len(lines) - len(shown))
                if remaining_count > 0:
                    content.append(f"... and {remaining_count} more users")
                await ctx.send("\n".join(content))
                return

            await ctx.send(
                "Unknown mode.\n"
                "Use one of: `last`, `me`, `user`, `role`, `roles`, `top`.\n"
                "Example: `!voicehours top 20 last 2 weeks`"
            )
            return

        week_since = now - timedelta(days=7)
        month_since = now - timedelta(days=30)
        summary = await self._repo.get_solo_voice_summary(
            ctx.guild.id,
            now=now,
            week_since=week_since,
            month_since=month_since,
        )
        week = {discord_id: row["week"] for discord_id, row in summary.items()}
        month = {discord_id: row["month"] for discord_id, row in summary.items()}
        all_time = {discord_id: row["all_time"] for discord_id, row in summary.items()}

        if not all_time and not week and not month:
            await ctx.send("No solo-channel voice logs found yet.")
            return

        all_ids = set(week) | set(month) | set(all_time)
        ordered_ids = sorted(all_ids, key=lambda user_id: all_time.get(user_id, 0.0), reverse=True)
        lines: list[str] = []
        for discord_id in ordered_ids:
            handle = handle_by_discord_id.get(discord_id)
            if handle is None:
                member = ctx.guild.get_member(discord_id)
                handle = member.display_name if member else str(discord_id)
            rank = _rank_prefix(len(lines) + 1)
            lines.append(
                f"{rank:<4} `{handle}` | {_hours(week.get(discord_id, 0.0))} | "
                f"{_hours(month.get(discord_id, 0.0))} | {_hours(all_time.get(discord_id, 0.0))}"
            )

        header = "**Rank | Handle | Last Week | Last Month | All Time**\n"
        body = "\n".join(lines[: config.voicehours_max_lines])
        extra = len(lines) - min(len(lines), config.voicehours_max_lines)
        if extra > 0:
            body += f"\n... and {extra} more users"
        rank_data = self._find_rank(all_time, ctx.author.id)
        footer = ""
        if rank_data is not None:
            rank, seconds = rank_data
            footer = f"\nYour all-time rank: **#{rank}** with **{_hours(seconds)}**"
        await ctx.send(header + body + footer)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VoiceLoggingCog(bot, getattr(bot, "user_repo"), getattr(bot, "guild_config")))

