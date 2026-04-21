from __future__ import annotations

from collections import Counter
import logging
import secrets
from statistics import median
import string

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
    """Codeforces handle verification and automatic role assignment."""

    def __init__(
        self,
        bot: commands.Bot,
        cf_client: CodeforcesClientBase,
        role_assigner: RoleAssignerBase,
        repo: UserRepositoryBase,
    ) -> None:
        self._cf = cf_client
        self._roles = role_assigner
        self._repo = repo
        self._pending: dict[int, tuple[str, str]] = {}

    @commands.command(name="verify")
    @commands.guild_only()
    async def verify(self, ctx: commands.Context, handle: str) -> None:
        """Start verification for a Codeforces handle."""
        if ctx.guild is None:
            return

        info = await self._cf.get_user(handle)
        if info is None:
            await ctx.send(f"Could not find Codeforces handle **{handle}**.")
            return

        code = _generate_code()
        self._pending[ctx.author.id] = (handle, code)

        embed = discord.Embed(
            title="Verification Started",
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

    @commands.command(name="confirm")
    @commands.guild_only()
    async def confirm(self, ctx: commands.Context) -> None:
        """Confirm your Codeforces verification after setting the code."""
        assert ctx.guild is not None and isinstance(ctx.author, discord.Member)

        pending = self._pending.get(ctx.author.id)
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
                f"First name mismatch.\n"
                f"Expected: `{expected_code}`\n"
                f"Found: `{info.first_name or '(empty)'}`\n\n"
                f"Update your CF profile and try **!confirm** again."
            )
            return

        del self._pending[ctx.author.id]

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

    @commands.command(name="update")
    @commands.guild_only()
    async def update(self, ctx: commands.Context) -> None:
        """Re-fetch your Codeforces rating and update your Discord role."""
        assert ctx.guild is not None and isinstance(ctx.author, discord.Member)

        record = await self._repo.get_by_discord_id(ctx.author.id, ctx.guild.id)
        if record is None:
            await ctx.send("You are not verified yet. Use **!verify <handle>** first.")
            return

        info = await self._cf.get_user(record.cf_handle)
        if info is None:
            await ctx.send("Could not reach the Codeforces API. Please try again later.")
            return

        record.rating = info.max_rating
        await self._repo.upsert(record)

        role = await self._roles.apply(ctx.author, ctx.guild, info.max_rating)
        role_text = f"**{role.name}**" if role else "*(none)*"

        embed = discord.Embed(
            title="Rating Updated",
            description=f"**{info.handle}** - max rating **{info.max_rating}** - role {role_text}.",
            colour=discord.Colour.blurple(),
        )
        await ctx.send(embed=embed)

    @commands.command(name="whois")
    @commands.guild_only()
    async def whois(self, ctx: commands.Context, handle: str) -> None:
        """Look up a Codeforces user live by handle."""
        info = await self._cf.get_user(handle)
        if info is None:
            await ctx.send(f"Could not find Codeforces handle **{handle}**.")
            return

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


async def setup(bot: commands.Bot) -> None:
    """Called by ``bot.load_extension("cogs.verification")``."""
    await bot.add_cog(
        VerificationCog(
            bot,
            getattr(bot, "cf_client"),
            getattr(bot, "role_assigner"),
            getattr(bot, "user_repo"),
        )
    )
    logger.info("Verification cog loaded")
