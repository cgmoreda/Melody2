# cogs/verification.py
# Discord cog for Codeforces handle verification & auto role assignment.
# Usage: loaded via bot.load_extension("cogs.verification") — see main.py.
#
# Commands
# --------
# !verify <cf_handle>   — start verification flow (sets code → checks CF API)
# !confirm              — confirm after setting your CF first name
# !update               — re-fetch rating and update role
# !whois [@user]        — look up someone's linked CF handle

from __future__ import annotations

import asyncio
from collections import Counter
import logging
import secrets
import string
from typing import Optional

import discord
from discord.ext import commands

from db.repository import UserRepositoryBase, VerifiedUser
from services.cf_client import CodeforcesClientBase
from services.role_assigner import RoleAssignerBase

logger = logging.getLogger(__name__)


def _generate_code(length: int = 6) -> str:
    """Return a short random alphanumeric token for verification."""
    alphabet = string.ascii_lowercase + string.digits
    return "CF-VERIFY-" + "".join(secrets.choice(alphabet) for _ in range(length))


class VerificationCog(commands.Cog, name="Verification"):
    """Codeforces handle verification & automatic role assignment.

    All heavy-lifting is delegated to injected services (DIP):
    *  ``cf_client``  — CF API calls
    *  ``role_assigner`` — rating → role mapping
    *  ``repo``        — persistent storage
    """

    def __init__(
        self,
        bot: commands.Bot,
        cf_client: CodeforcesClientBase,
        role_assigner: RoleAssignerBase,
        repo: UserRepositoryBase,
    ) -> None:
        self.bot = bot
        self._cf = cf_client
        self._roles = role_assigner
        self._repo = repo

        # Maps discord_user_id → (cf_handle, expected_code)
        self._pending: dict[int, tuple[str, str]] = {}

    # ── !verify ────────────────────────────────────────────────

    @commands.command(name="verify")
    @commands.guild_only()
    async def verify(self, ctx: commands.Context, handle: str) -> None:
        """Start verification for a Codeforces handle."""
        assert ctx.guild is not None  # guild_only guard

        # Quick sanity check: does the handle exist?
        info = await self._cf.get_user(handle)
        if info is None:
            await ctx.send(f"❌ Could not find Codeforces handle **{handle}**.")
            return

        code = _generate_code()
        self._pending[ctx.author.id] = (handle, code)

        embed = discord.Embed(
            title="🔑 Verification Started",
            description=(
                f"To prove you own **{handle}**, please do the following:\n\n"
                f"1. Go to [your Codeforces settings](https://codeforces.com/settings/social)\n"
                f"2. Set your **First name** to:\n```\n{code}\n```\n"
                f"3. Save, then type **!confirm** here."
            ),
            colour=discord.Colour.gold(),
        )
        embed.set_footer(text="The code expires when you start a new verification.")
        await ctx.send(embed=embed)

    # ── !confirm ───────────────────────────────────────────────

    @commands.command(name="confirm")
    @commands.guild_only()
    async def confirm(self, ctx: commands.Context) -> None:
        """Confirm your Codeforces verification after setting the code."""
        assert ctx.guild is not None and isinstance(ctx.author, discord.Member)

        pending = self._pending.get(ctx.author.id)
        if pending is None:
            await ctx.send("⚠️ You have no pending verification. Use **!verify <handle>** first.")
            return

        handle, expected_code = pending

        info = await self._cf.get_user(handle)
        if info is None:
            await ctx.send("❌ Could not reach the Codeforces API. Please try again later.")
            return

        if info.first_name != expected_code:
            await ctx.send(
                f"❌ First name mismatch.\n"
                f"Expected: `{expected_code}`\n"
                f"Found: `{info.first_name or '(empty)'}`\n\n"
                f"Update your CF profile and try **!confirm** again."
            )
            return

        # ✅ Verified — persist, assign role, clean up.
        del self._pending[ctx.author.id]

        user = VerifiedUser(
            discord_id=ctx.author.id,
            cf_handle=info.handle,
            rating=info.rating,
            guild_id=ctx.guild.id,
        )
        await self._repo.upsert(user)

        role = await self._roles.apply(ctx.author, ctx.guild, info.rating)
        role_text = f" → assigned role **{role.name}**" if role else ""

        embed = discord.Embed(
            title="✅ Verification Successful",
            description=(
                f"**{info.handle}** (rating **{info.rating}**) is now linked "
                f"to {ctx.author.mention}{role_text}."
            ),
            colour=discord.Colour.green(),
        )
        await ctx.send(embed=embed)
        logger.info("Verified %s as %s (rating %d)", ctx.author, info.handle, info.rating)

    # ── !update ────────────────────────────────────────────────

    @commands.command(name="update")
    @commands.guild_only()
    async def update(self, ctx: commands.Context) -> None:
        """Re-fetch your Codeforces rating and update your Discord role."""
        assert ctx.guild is not None and isinstance(ctx.author, discord.Member)

        record = await self._repo.get_by_discord_id(ctx.author.id, ctx.guild.id)
        if record is None:
            await ctx.send("⚠️ You are not verified yet. Use **!verify <handle>** first.")
            return

        info = await self._cf.get_user(record.cf_handle)
        if info is None:
            await ctx.send("❌ Could not reach the Codeforces API. Please try again later.")
            return

        record.rating = info.rating
        await self._repo.upsert(record)

        role = await self._roles.apply(ctx.author, ctx.guild, info.rating)
        role_text = f"**{role.name}**" if role else "*(none)*"

        embed = discord.Embed(
            title="🔄 Rating Updated",
            description=(
                f"**{info.handle}** — rating **{info.rating}** — role {role_text}."
            ),
            colour=discord.Colour.blurple(),
        )
        await ctx.send(embed=embed)

    # ── !whois ─────────────────────────────────────────────────

    @commands.command(name="whois")
    @commands.guild_only()
    async def whois(self, ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        """Look up the Codeforces handle linked to a Discord user."""
        assert ctx.guild is not None
        target = member or ctx.author
        assert isinstance(target, discord.Member)

        record = await self._repo.get_by_discord_id(target.id, ctx.guild.id)
        if record is None:
            await ctx.send(f"⚠️ {target.mention} is not verified.")
            return

        embed = discord.Embed(
            title=f"🔍 {target.display_name}",
            description=(
                f"**CF Handle:** [{record.cf_handle}](https://codeforces.com/profile/{record.cf_handle})\n"
                f"**Rating:** {record.rating}\n"
                f"**Max Rating:** {record.max_rating}"
            ),
            colour=discord.Colour.blurple(),
        )
        await ctx.send(embed=embed)

    @commands.command(name="stats")
    @commands.guild_only()
    async def stats(self, ctx: commands.Context, handle: str) -> None:
        """Show live contest and submission stats for a Codeforces handle."""
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

        if history:
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
            for row in history[-5:]:
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
        else:
            embed.add_field(name="Contest Stats", value="No rated contest history found.", inline=False)

        if submissions:
            solved = sum(1 for row in submissions if row.verdict == "OK")
            total = len(submissions)
            accepted_rate = (solved / total) * 100
            solved_problem_keys = {
                row.problem_key for row in submissions if row.verdict == "OK" and row.problem_key is not None
            }
            unique_solved = len(solved_problem_keys)
            verdicts = Counter(row.verdict or "UNKNOWN" for row in submissions)
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
        else:
            embed.add_field(name="Submission Activity", value="No recent submissions found.", inline=False)

        if info.avatar_url:
            embed.set_thumbnail(url=info.avatar_url)
        embed.set_footer(text="Data fetched live from Codeforces")
        await ctx.send(embed=embed)

    @commands.command(name="roundchanges", aliases=["lastround"])
    @commands.guild_only()
    async def roundchanges(self, ctx: commands.Context) -> None:
        """Show rating changes for verified users in the latest recent round."""
        assert ctx.guild is not None

        users = await self._repo.get_all(ctx.guild.id)
        if not users:
            await ctx.send("No verified users found in this server.")
            return

        unique_handles = sorted({user.cf_handle for user in users})

        sem = asyncio.Semaphore(8)

        async def _fetch(handle: str) -> tuple[str, list]:
            async with sem:
                history = await self._cf.get_rating_history(handle)
                return handle, history

        history_rows = await asyncio.gather(*[_fetch(handle) for handle in unique_handles])
        history_by_handle = {handle: history for handle, history in history_rows}

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
                    f"{identity} (`{user.cf_handle}`): **{sign}{delta}** "
                    f"({row.old_rating} -> {row.new_rating}, rank {row.rank})",
                )
            )

        participants.sort(key=lambda item: item[0], reverse=True)

        lines: list[str] = [line for _, line in participants]
        lines.extend(non_participants)
        if not lines:
            await ctx.send("No rating updates found for the latest round.")
            return

        max_lines = 30
        displayed = lines[:max_lines]
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

        target = channel or ctx.channel
        if not isinstance(target, discord.TextChannel):
            await ctx.send("This command must target a text channel.")
            return

        created = await self._reminders.enable_channel(ctx.guild.id, target.id)
        if created:
            await ctx.send(f"Contest reminders enabled in {target.mention}.")
        else:
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

        target = channel or ctx.channel
        if not isinstance(target, discord.TextChannel):
            await ctx.send("This command must target a text channel.")
            return

        removed = await self._reminders.disable_channel(ctx.guild.id, target.id)
        if removed:
            await ctx.send(f"Contest reminders disabled in {target.mention}.")
        else:
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

        target = channel or ctx.channel
        if not isinstance(target, discord.TextChannel):
            await ctx.send("This command must target a text channel.")
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

        contests = await self._reminders.get_upcoming_div_contests(limit=3)
        if not contests:
            await ctx.send("No upcoming Div contests found right now.")
            return

        lines = []
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
        embed.set_footer(text="Showing up to 3 upcoming Div contests")
        await ctx.send(embed=embed)

    # ── error handler ──────────────────────────────────────────

    @verify.error
    async def verify_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Usage: **!verify <codeforces_handle>**")
        else:
            raise error


# ── Extension entry-point ──────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    """Called by ``bot.load_extension("cogs.verification")``."""
    # Resolve dependencies wired by main.py onto the bot instance.
    cf_client = getattr(bot, "cf_client")
    role_assigner = getattr(bot, "role_assigner")
    user_repo = getattr(bot, "user_repo")

    await bot.add_cog(VerificationCog(bot, cf_client, role_assigner, user_repo))
    logger.info("Verification cog loaded")
