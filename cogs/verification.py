from __future__ import annotations

import asyncio
from collections import Counter
import logging
import secrets
from statistics import median
import string
from typing import Optional

import discord
from discord.ext import commands

from db.repository import UserRepositoryBase, VerifiedUser
from services.cf_client import CFContestChange, CFSubmission, CFUserInfo, CodeforcesClientBase
from services.contest_reminder import ContestReminderService
from services.role_assigner import RoleAssignerBase

logger = logging.getLogger(__name__)

MAX_ROUNDCHANGES_LINES = 30
MAX_RECENT_CONTEST_LINES = 5
MAX_REMINDER_PREVIEW = 3


def _generate_code(length: int = 6) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "CF-VERIFY-" + "".join(secrets.choice(alphabet) for _ in range(length))


def _whois_colour(max_rating: int) -> discord.Colour:
    if max_rating >= 2400:
        return discord.Colour.red()
    if max_rating >= 2100:
        return discord.Colour.orange()
    if max_rating >= 1900:
        return discord.Colour.purple()
    if max_rating >= 1600:
        return discord.Colour.blue()
    if max_rating >= 1200:
        return discord.Colour.green()
    return discord.Colour.light_grey()


class VerificationCog(commands.Cog, name="Verification"):
    def __init__(
        self,
        cf_client: CodeforcesClientBase,
        role_assigner: RoleAssignerBase,
        repo: UserRepositoryBase,
        reminder_service: Optional[ContestReminderService],
    ) -> None:
        self._cf = cf_client
        self._roles = role_assigner
        self._repo = repo
        self._reminders = reminder_service
        self._pending: dict[tuple[int, int], tuple[str, str]] = {}

    @staticmethod
    def _pending_key(guild_id: int, user_id: int) -> tuple[int, int]:
        return guild_id, user_id

    @staticmethod
    def _build_verify_embed(handle: str, code: str) -> discord.Embed:
        embed = discord.Embed(
            title="Verification Started",
            description=(
                f"To prove you own **{handle}**, please do the following:\n\n"
                "1. Go to [your Codeforces settings](https://codeforces.com/settings/social)\n"
                f"2. Set your **First name** to:\n```\n{code}\n```\n"
                "3. Save, then type **!confirm** here."
            ),
            colour=discord.Colour.gold(),
        )
        embed.set_footer(text="The code expires when you start a new verification.")
        return embed

    @staticmethod
    def _build_whois_embed(info: CFUserInfo) -> discord.Embed:
        rank = (info.rank or "unrated").title()
        max_rank = (info.max_rank or "unrated").title()
        location = ", ".join(part for part in [info.city, info.country] if part) or "Unknown"
        organization = info.organization or "Not specified"

        embed = discord.Embed(
            title=f"Codeforces: {info.handle}",
            url=f"https://codeforces.com/profile/{info.handle}",
            description=f"Live profile lookup for **{info.handle}**",
            colour=_whois_colour(info.max_rating),
        )
        embed.add_field(name="Rank", value=rank, inline=True)
        embed.add_field(name="Max Rank", value=max_rank, inline=True)
        embed.add_field(name="Contribution", value=str(info.contribution), inline=True)
        embed.add_field(name="Current Rating", value=str(info.rating), inline=True)
        embed.add_field(name="Max Rating", value=str(info.max_rating), inline=True)
        embed.add_field(name="Friends", value=str(info.friend_of_count), inline=True)
        embed.add_field(name="Location", value=location, inline=False)
        embed.add_field(name="Organization", value=organization, inline=False)
        if info.avatar_url:
            embed.set_thumbnail(url=info.avatar_url)
        if info.title_photo_url:
            embed.set_image(url=info.title_photo_url)
        embed.set_footer(text="Data fetched live from Codeforces")
        return embed

    @staticmethod
    def _add_contest_stats(embed: discord.Embed, info: CFUserInfo, history: list[CFContestChange]) -> None:
        if not history:
            embed.add_field(name="Contest Stats", value="No rated contest history found.", inline=False)
            return

        ranks = [row.rank for row in history if row.rank > 0]
        contest_count = len(history)
        best_rank = min(ranks) if ranks else 0
        median_rank = int(median(ranks)) if ranks else 0
        current_rating = history[-1].new_rating

        deltas = [row.new_rating - row.old_rating for row in history]
        positive_rounds = sum(1 for delta in deltas if delta > 0)
        negative_rounds = sum(1 for delta in deltas if delta < 0)
        neutral_rounds = contest_count - positive_rounds - negative_rounds

        deltas_after_first_five = deltas[5:]
        best_gain = max(deltas_after_first_five) if deltas_after_first_five else 0
        worst_drop = min(deltas) if deltas else 0

        recent_window = deltas[-10:]
        recent_delta = sum(recent_window)
        recent_delta_sign = "+" if recent_delta >= 0 else ""
        best_gain_sign = "+" if best_gain >= 0 else ""

        recent_lines: list[str] = []
        for row in history[-MAX_RECENT_CONTEST_LINES:]:
            change = row.new_rating - row.old_rating
            sign = "+" if change >= 0 else ""
            recent_lines.append(f"`{row.contest_name[:30]}`: {row.rank} ({sign}{change})")

        embed.add_field(
            name="Contest Quality",
            value=(
                f"Contests: **{contest_count}**\n"
                f"Best Rank: **{best_rank}**\n"
                f"Median Rank: **{median_rank}**\n"
                f"Current / Max: **{current_rating} / {info.max_rating}**"
            ),
            inline=True,
        )
        embed.add_field(
            name="Momentum",
            value=(
                f"Last 10 Delta: **{recent_delta_sign}{recent_delta}**\n"
                f"Best Gain: **{best_gain_sign}{best_gain}**\n"
                f"Worst Drop: **{worst_drop}**\n"
                f"+/-/0: **{positive_rounds}/{negative_rounds}/{neutral_rounds}**"
            ),
            inline=True,
        )
        embed.add_field(
            name="Last 5 Contests",
            value="\n".join(recent_lines) if recent_lines else "No recent contests.",
            inline=False,
        )

    @staticmethod
    def _add_submission_stats(embed: discord.Embed, submissions: list[CFSubmission]) -> None:
        if not submissions:
            embed.add_field(name="Submission Activity", value="No recent submissions found.", inline=False)
            return

        solved = sum(1 for row in submissions if row.verdict == "OK")
        total = len(submissions)
        accepted_rate = (solved / total) * 100

        solved_problem_keys = {
            row.problem_key for row in submissions if row.verdict == "OK" and row.problem_key is not None
        }
        unique_solved = len(solved_problem_keys)
        fail_count = total - solved
        retry_factor = (solved / unique_solved) if unique_solved else 0.0

        solved_tag_counter: Counter[str] = Counter()
        for row in submissions:
            if row.verdict == "OK":
                solved_tag_counter.update(row.tags)

        top_tags = ", ".join(
            f"{tag}({count})" for tag, count in solved_tag_counter.most_common(5)
        ) or "N/A"

        embed.add_field(
            name="Solving Stats (last 500 submissions)",
            value=(
                f"Accepted: **{solved}/{total}** ({accepted_rate:.1f}%)\n"
                f"Unique Solved: **{unique_solved}**\n"
                f"Failed Attempts: **{fail_count}**\n"
                f"Avg Accepted/Subproblem: **{retry_factor:.2f}**\n"
                f"Top Solved Tags: {top_tags}"
            ),
            inline=False,
        )

    async def _resolve_reminder_channel(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel],
    ) -> Optional[discord.TextChannel]:
        target = channel or ctx.channel
        if not isinstance(target, discord.TextChannel):
            await ctx.send("This command must target a text channel.")
            return None
        return target

    async def _fetch_latest_histories(self, handles: list[str]) -> dict[str, list[CFContestChange]]:
        sem = asyncio.Semaphore(8)

        async def _fetch(handle: str) -> tuple[str, list[CFContestChange]]:
            async with sem:
                history = await self._cf.get_rating_history(handle)
                return handle, history

        rows = await asyncio.gather(*[_fetch(handle) for handle in handles])
        return {handle: history for handle, history in rows}

    @commands.command(name="verify")
    @commands.guild_only()
    async def verify(self, ctx: commands.Context, handle: str) -> None:
        if ctx.guild is None:
            return

        info = await self._cf.get_user(handle)
        if info is None:
            await ctx.send(f"Could not find Codeforces handle **{handle}**.")
            return

        code = _generate_code()
        self._pending[self._pending_key(ctx.guild.id, ctx.author.id)] = (handle, code)
        await ctx.send(embed=self._build_verify_embed(handle, code))

    @commands.command(name="confirm")
    @commands.guild_only()
    async def confirm(self, ctx: commands.Context) -> None:
        assert ctx.guild is not None and isinstance(ctx.author, discord.Member)

        key = self._pending_key(ctx.guild.id, ctx.author.id)
        pending = self._pending.get(key)
        if pending is None:
            await ctx.send("You have no pending verification. Use **!verify <handle>** first.")
            return

        handle, expected_code = pending
        info = await self._cf.get_user(handle)
        if info is None:
            await ctx.send("Could not reach the Codeforces API. Please try again later.")
            return

        if info.first_name != expected_code:
            await ctx.send(
                "First name mismatch.\n"
                f"Expected: `{expected_code}`\n"
                f"Found: `{info.first_name or '(empty)'}`\n\n"
                "Update your CF profile and try **!confirm** again."
            )
            return

        del self._pending[key]

        user = VerifiedUser(
            discord_id=ctx.author.id,
            cf_handle=info.handle,
            rating=info.max_rating,
            guild_id=ctx.guild.id,
        )
        await self._repo.upsert(user)

        role = await self._roles.apply(ctx.author, ctx.guild, info.max_rating)
        role_text = f" and assigned role **{role.name}**" if role else ""

        embed = discord.Embed(
            title="Verification Successful",
            description=(
                f"**{info.handle}** (max rating **{info.max_rating}**) is now linked "
                f"to {ctx.author.mention}{role_text}."
            ),
            colour=discord.Colour.green(),
        )
        await ctx.send(embed=embed)
        logger.info("Verified %s as %s (max rating %d)", ctx.author, info.handle, info.max_rating)

    @commands.command(name="updaterating", aliases=["update"])
    @commands.guild_only()
    async def updaterating(self, ctx: commands.Context) -> None:
        assert ctx.guild is not None and isinstance(ctx.author, discord.Member)

        record = await self._repo.get_by_discord_id(ctx.author.id, ctx.guild.id)
        if record is None:
            await ctx.send("You are not verified yet. Use **!verify <handle>** first.")
            return

        info = await self._cf.get_user(record.cf_handle)
        if info is None:
            await ctx.send("Could not reach the Codeforces API. Please try again later.")
            return

        old_rating = record.rating
        old_handle = record.cf_handle
        old_role_rule = self._roles.role_for(old_rating)
        old_role_name = old_role_rule.name if old_role_rule else "none"

        updated_record = VerifiedUser(
            discord_id=record.discord_id,
            cf_handle=info.handle,
            rating=info.max_rating,
            guild_id=record.guild_id,
        )
        await self._repo.upsert(updated_record)

        role = await self._roles.apply(ctx.author, ctx.guild, info.max_rating)
        new_role_rule = self._roles.role_for(info.max_rating)
        new_role_name = new_role_rule.name if new_role_rule else "none"
        role_text = f"**{new_role_name}**" if role else f"**{new_role_name}** (not applied)"

        rating_delta = info.max_rating - old_rating
        delta_sign = "+" if rating_delta >= 0 else ""
        if old_rating == info.max_rating and old_handle == info.handle:
            summary = "No change detected."
        else:
            summary = f"Max rating changed by **{delta_sign}{rating_delta}**."

        embed = discord.Embed(
            title="Rating Updated",
            description=(
                f"**Handle:** {old_handle} -> {info.handle}\n"
                f"**Max Rating:** {old_rating} -> {info.max_rating}\n"
                f"**Role:** {old_role_name} -> {role_text}\n"
                f"{summary}"
            ),
            colour=discord.Colour.blurple(),
        )
        await ctx.send(embed=embed)

    @commands.command(name="whois")
    @commands.guild_only()
    async def whois(self, ctx: commands.Context, handle: str) -> None:
        info = await self._cf.get_user(handle)
        if info is None:
            await ctx.send(f"Could not find Codeforces handle **{handle}**.")
            return

        await ctx.send(embed=self._build_whois_embed(info))

    @commands.command(name="stats")
    @commands.guild_only()
    async def stats(self, ctx: commands.Context, handle: str) -> None:
        info = await self._cf.get_user(handle)
        if info is None:
            await ctx.send(f"Could not find Codeforces handle **{handle}**.")
            return

        history = await self._cf.get_rating_history(info.handle)
        submissions = await self._cf.get_recent_submissions(info.handle, count=500)

        embed = discord.Embed(
            title=f"Stats: {info.handle}",
            url=f"https://codeforces.com/profile/{info.handle}",
            description="Live contest performance and solving quality",
            colour=_whois_colour(info.max_rating),
        )
        self._add_contest_stats(embed, info, history)
        self._add_submission_stats(embed, submissions)

        if info.avatar_url:
            embed.set_thumbnail(url=info.avatar_url)
        embed.set_footer(text="Data fetched live from Codeforces")
        await ctx.send(embed=embed)

    @commands.command(name="roundchanges", aliases=["lastround"])
    @commands.guild_only()
    async def roundchanges(self, ctx: commands.Context) -> None:
        assert ctx.guild is not None

        users = await self._repo.get_all(ctx.guild.id)
        if not users:
            await ctx.send("No verified users found in this server.")
            return

        unique_handles = sorted({user.cf_handle for user in users})
        history_by_handle = await self._fetch_latest_histories(unique_handles)

        latest_entries = [history[-1] for history in history_by_handle.values() if history]
        if not latest_entries:
            await ctx.send("Could not fetch rating history for verified users right now.")
            return

        target_contest_id = max(entry.contest_id for entry in latest_entries)
        target_contest_name = next(
            (entry.contest_name for entry in latest_entries if entry.contest_id == target_contest_id),
            f"Contest {target_contest_id}",
        )

        participants: list[tuple[int, str]] = []
        non_participants: list[str] = []
        for user in users:
            history = history_by_handle.get(user.cf_handle, [])
            row = next((item for item in reversed(history) if item.contest_id == target_contest_id), None)
            member = ctx.guild.get_member(user.discord_id)
            identity = member.mention if member else f"<@{user.discord_id}>"

            if row is None:
                non_participants.append(f"{identity} (`{user.cf_handle}`): did not participate")
                continue

            delta = row.new_rating - row.old_rating
            sign = "+" if delta >= 0 else ""
            participants.append(
                (
                    delta,
                    (
                        f"{identity} (`{user.cf_handle}`): **{sign}{delta}** "
                        f"({row.old_rating} -> {row.new_rating}, rank {row.rank})"
                    ),
                )
            )

        participants.sort(key=lambda item: item[0], reverse=True)

        lines: list[str] = [line for _, line in participants]
        lines.extend(non_participants)
        if not lines:
            await ctx.send("No rating updates found for the latest round.")
            return

        displayed = lines[:MAX_ROUNDCHANGES_LINES]
        hidden_count = len(lines) - len(displayed)

        embed = discord.Embed(
            title="Server Round Changes",
            description="\n".join(displayed),
            colour=discord.Colour.gold(),
        )
        embed.add_field(name="Round", value=target_contest_name, inline=False)
        embed.add_field(name="Contest ID", value=str(target_contest_id), inline=True)
        embed.add_field(name="Verified Users", value=str(len(users)), inline=True)

        if hidden_count > 0:
            embed.set_footer(text=f"{hidden_count} more users not shown due to message length.")
        else:
            embed.set_footer(text="Data fetched live from Codeforces.")
        await ctx.send(embed=embed)

    @commands.group(name="reminder", invoke_without_command=True)
    @commands.guild_only()
    async def reminder(self, ctx: commands.Context) -> None:
        await ctx.send("Usage: **!reminder <enable|disable|status|next> [#channel]**")

    @reminder.command(name="enable")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def reminder_enable(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        assert ctx.guild is not None
        if self._reminders is None:
            await ctx.send("Reminder service is not available.")
            return

        target = await self._resolve_reminder_channel(ctx, channel)
        if target is None:
            return

        created = await self._reminders.enable_channel(ctx.guild.id, target.id)
        if created:
            await ctx.send(f"Contest reminders enabled in {target.mention}.")
            return
        await ctx.send(f"Contest reminders are already enabled in {target.mention}.")

    @reminder.command(name="disable")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def reminder_disable(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        assert ctx.guild is not None
        if self._reminders is None:
            await ctx.send("Reminder service is not available.")
            return

        target = await self._resolve_reminder_channel(ctx, channel)
        if target is None:
            return

        removed = await self._reminders.disable_channel(ctx.guild.id, target.id)
        if removed:
            await ctx.send(f"Contest reminders disabled in {target.mention}.")
            return
        await ctx.send(f"Contest reminders were not enabled in {target.mention}.")

    @reminder.command(name="status")
    @commands.guild_only()
    async def reminder_status(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        assert ctx.guild is not None
        if self._reminders is None:
            await ctx.send("Reminder service is not available.")
            return

        target = await self._resolve_reminder_channel(ctx, channel)
        if target is None:
            return

        enabled = await self._reminders.is_channel_enabled(ctx.guild.id, target.id)
        state = "enabled" if enabled else "disabled"
        await ctx.send(f"Contest reminders are **{state}** in {target.mention}.")

    @reminder.command(name="next")
    @commands.guild_only()
    async def reminder_next(self, ctx: commands.Context) -> None:
        if self._reminders is None:
            await ctx.send("Reminder service is not available.")
            return

        contests = await self._reminders.get_upcoming_div_contests(limit=MAX_REMINDER_PREVIEW)
        if not contests:
            await ctx.send("No upcoming Div contests found right now.")
            return

        lines: list[str] = []
        for idx, contest in enumerate(contests, start=1):
            ts = contest.start_time_seconds
            lines.append(
                f"{idx}. [{contest.name}](https://codeforces.com/contest/{contest.contest_id}) - "
                f"<t:{ts}:R> (<t:{ts}:f>)"
            )

        embed = discord.Embed(
            title="Upcoming Div Contests",
            description="\n".join(lines),
            colour=discord.Colour.gold(),
        )
        embed.set_footer(text=f"Showing up to {MAX_REMINDER_PREVIEW} upcoming Div contests")
        await ctx.send(embed=embed)

    @verify.error
    async def verify_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Usage: **!verify <codeforces_handle>**")
            return
        raise error

    @whois.error
    async def whois_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Usage: **!whois <codeforces_handle>**")
            return
        raise error

    @stats.error
    async def stats_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Usage: **!stats <codeforces_handle>**")
            return
        raise error

    @reminder.error
    async def reminder_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You need the **Manage Server** permission for this reminder action.")
            return
        raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(
        VerificationCog(
            getattr(bot, "cf_client"),
            getattr(bot, "role_assigner"),
            getattr(bot, "user_repo"),
            getattr(bot, "contest_reminder", None),
        )
    )
    logger.info("Verification cog loaded")
