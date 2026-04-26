from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
import logging
import secrets
from statistics import median
import string
from typing import Optional

import discord
from discord.ext import commands

from db.repository import VerificationRepository, VerifiedUser
from services.discord_output import (
    DISCORD_EMBED_FIELD_VALUE_LIMIT,
    clip_embed_description,
    clip_text,
    split_embed_description_chunks,
)
from services.cf_client import CFContestChange, CFRequestError, CFSubmission, CFUserInfo, CodeforcesClientBase
from services.contest_reminder import ContestReminderService
from services.guild_config import GuildConfigService
from services.role_assigner import RoleAssignerBase

logger = logging.getLogger(__name__)

MAX_RECENT_CONTEST_LINES = 5
PENDING_VERIFICATION_EXPIRY_MINUTES = 15


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
        repo: VerificationRepository,
        config_service: GuildConfigService,
        reminder_service: Optional[ContestReminderService],
    ) -> None:
        self._cf = cf_client
        self._roles = role_assigner
        self._repo = repo
        self._config = config_service
        self._reminders = reminder_service

    @staticmethod
    def _build_roundchanges_embeds(
        *,
        displayed_lines: list[str],
        contest_name: str,
        contest_id: int,
        verified_users: int,
        hidden_count: int,
    ) -> list[discord.Embed]:
        descriptions = split_embed_description_chunks("\n".join(displayed_lines))
        total_pages = len(descriptions)
        round_value = clip_text(contest_name, limit=DISCORD_EMBED_FIELD_VALUE_LIMIT)

        embeds: list[discord.Embed] = []
        for index, description in enumerate(descriptions, start=1):
            title = "Server Round Changes" if total_pages == 1 else f"Server Round Changes ({index}/{total_pages})"
            embed = discord.Embed(
                title=title,
                description=clip_embed_description(description),
                colour=discord.Colour.gold(),
            )
            embed.add_field(name="Round", value=round_value or "-", inline=False)
            embed.add_field(name="Contest ID", value=str(contest_id), inline=True)
            embed.add_field(name="Verified Users", value=str(verified_users), inline=True)

            if index == total_pages:
                if hidden_count > 0:
                    embed.set_footer(text=f"{hidden_count} more users not shown due to message length.")
                else:
                    embed.set_footer(text="Data fetched live from Codeforces.")
            else:
                embed.set_footer(text=f"Page {index}/{total_pages}")
            embeds.append(embed)
        return embeds

    @staticmethod
    def _build_verify_embed(handle: str, code: str, expires_in_minutes: int) -> discord.Embed:
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
        embed.set_footer(text=f"The code expires in {expires_in_minutes} minutes.")
        return embed

    @staticmethod
    def _cf_error_message(error: CFRequestError) -> str:
        status_text = f", status {error.http_status}" if error.http_status is not None else ""
        lines = [f"Request failed: endpoint {error.endpoint}{status_text} ({error.failure_kind})."]
        if error.requested_url and error.failure_kind != "non_ok":
            lines.append(f"URL: `{error.requested_url}`")
        lines.append("Please try again in a minute.")
        return "\n".join(lines)

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
        """Start Codeforces handle verification for your Discord account."""
        if ctx.guild is None:
            return

        try:
            info = await self._cf.get_user(handle)
        except CFRequestError as exc:
            await ctx.send(self._cf_error_message(exc))
            return
        if info is None:
            await ctx.send(f"Could not find Codeforces handle **{handle}**.")
            return

        code = _generate_code()
        created_at = datetime.now(tz=UTC)
        expires_at = created_at + timedelta(minutes=PENDING_VERIFICATION_EXPIRY_MINUTES)
        await self._repo.upsert_pending_verification(
            guild_id=ctx.guild.id,
            discord_id=ctx.author.id,
            cf_handle=info.handle,
            verification_code=code,
            created_at=created_at,
            expires_at=expires_at,
        )
        await ctx.send(embed=self._build_verify_embed(info.handle, code, PENDING_VERIFICATION_EXPIRY_MINUTES))

    @commands.command(name="confirm")
    @commands.guild_only()
    async def confirm(self, ctx: commands.Context) -> None:
        """Confirm verification after setting your temporary code on Codeforces."""
        assert ctx.guild is not None and isinstance(ctx.author, discord.Member)

        pending = await self._repo.get_pending_verification(ctx.guild.id, ctx.author.id)
        if pending is None:
            await ctx.send("You have no pending verification. Use **!verify <handle>** first.")
            return

        now = datetime.now(tz=UTC)
        if pending.expires_at <= now:
            await self._repo.delete_pending_verification(ctx.guild.id, ctx.author.id)
            await ctx.send("Your pending verification code expired. Use **!verify <handle>** to start again.")
            return

        try:
            info = await self._cf.get_user(pending.cf_handle)
        except CFRequestError as exc:
            await ctx.send(self._cf_error_message(exc))
            return
        if info is None:
            await ctx.send(
                f"Codeforces handle **{pending.cf_handle}** was not found. "
                "Run **!verify <handle>** again."
            )
            return

        if info.first_name != pending.verification_code:
            await ctx.send(
                "First name mismatch.\n"
                f"Expected: `{pending.verification_code}`\n"
                f"Found: `{info.first_name or '(empty)'}`\n\n"
                "Update your CF profile and try **!confirm** again."
            )
            return

        await self._repo.delete_pending_verification(ctx.guild.id, ctx.author.id)

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
        """Refresh your linked Codeforces rating and update your role."""
        assert ctx.guild is not None and isinstance(ctx.author, discord.Member)

        record = await self._repo.get_by_discord_id(ctx.author.id, ctx.guild.id)
        if record is None:
            await ctx.send("You are not verified yet. Use **!verify <handle>** first.")
            return

        try:
            info = await self._cf.get_user(record.cf_handle)
        except CFRequestError as exc:
            await ctx.send(self._cf_error_message(exc))
            return
        if info is None:
            await ctx.send(
                f"Linked handle **{record.cf_handle}** was not found on Codeforces. "
                "Run **!verify <handle>** to relink."
            )
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
        """Show live Codeforces profile details for a handle."""
        try:
            info = await self._cf.get_user(handle)
        except CFRequestError as exc:
            await ctx.send(self._cf_error_message(exc))
            return
        if info is None:
            await ctx.send(f"Could not find Codeforces handle **{handle}**.")
            return

        await ctx.send(embed=self._build_whois_embed(info))

    @commands.command(name="stats")
    @commands.guild_only()
    async def stats(self, ctx: commands.Context, handle: str) -> None:
        """Show contest and submission statistics for a Codeforces handle."""
        try:
            info = await self._cf.get_user(handle)
        except CFRequestError as exc:
            await ctx.send(self._cf_error_message(exc))
            return
        if info is None:
            await ctx.send(f"Could not find Codeforces handle **{handle}**.")
            return

        try:
            history = await self._cf.get_rating_history(info.handle)
            submissions = await self._cf.get_recent_submissions(info.handle, count=500)
        except CFRequestError as exc:
            await ctx.send(self._cf_error_message(exc))
            return

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
        """Show latest round rating changes for verified server members."""
        assert ctx.guild is not None

        config = await self._config.get(ctx.guild.id)
        users = await self._repo.get_all(ctx.guild.id)
        if not users:
            await ctx.send("No verified users found in this server.")
            return

        unique_handles = sorted({user.cf_handle for user in users})
        try:
            history_by_handle = await self._fetch_latest_histories(unique_handles)
        except CFRequestError as exc:
            await ctx.send(self._cf_error_message(exc))
            return

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

        displayed = lines[: config.roundchanges_max_lines]
        hidden_count = len(lines) - len(displayed)

        for embed in self._build_roundchanges_embeds(
            displayed_lines=displayed,
            contest_name=target_contest_name,
            contest_id=target_contest_id,
            verified_users=len(users),
            hidden_count=hidden_count,
        ):
            await ctx.send(embed=embed)

    @commands.group(name="reminder", invoke_without_command=True)
    @commands.guild_only()
    async def reminder(self, ctx: commands.Context) -> None:
        """Manage Codeforces and AtCoder contest reminders for channels."""
        await ctx.send("Usage: **!reminder <enable|disable|status|next [platform]> [#channel]**")

    @reminder.command(name="enable")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def reminder_enable(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        """Enable contest reminders in a channel."""
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
        """Disable contest reminders in a channel."""
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
        """Check whether reminders are enabled in a channel."""
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
    async def reminder_next(self, ctx: commands.Context, platform: str = "codeforces") -> None:
        """Show upcoming contests for a given platform.
        
        Example: `!reminder next atcoder` or `!reminder next codeforces`
        """
        platform = platform.lower()
        if platform not in ("codeforces", "atcoder", "cf", "ac"):
            await ctx.send("Unsupported platform. Use `codeforces` or `atcoder`.")
            return
            
        platform_mapping = {"cf": "codeforces", "ac": "atcoder"}
        canonical_platform = platform_mapping.get(platform, platform)

        assert ctx.guild is not None
        if self._reminders is None:
            await ctx.send("Reminder service is not available.")
            return

        config = await self._config.get(ctx.guild.id)
        contests = await self._reminders.get_upcoming_contests(platform=canonical_platform, limit=config.reminder_preview_limit)
        
        display_platform = "Codeforces Div" if canonical_platform == "codeforces" else "AtCoder"

        if not contests:
            await ctx.send(f"No upcoming {display_platform} contests found right now.")
            return

        lines: list[str] = []
        for idx, contest in enumerate(contests, start=1):
            ts = contest.start_time_seconds
            
            if canonical_platform == "codeforces":
                url = f"https://codeforces.com/contest/{contest.contest_id}"
            else:
                url = f"https://atcoder.jp/contests/{contest.contest_id}"
                
            lines.append(
                f"{idx}. [{contest.name}]({url}) - "
                f"<t:{ts}:R> (<t:{ts}:f>)"
            )

        embed = discord.Embed(
            title=f"Upcoming {display_platform} Contests",
            description="\n".join(lines),
            colour=discord.Colour.gold(),
        )
        embed.set_footer(text=f"Showing up to {config.reminder_preview_limit} upcoming {display_platform} contests")
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
            getattr(bot, "guild_config"),
            getattr(bot, "contest_reminder", None),
        )
    )
    logger.info("Verification cog loaded")
