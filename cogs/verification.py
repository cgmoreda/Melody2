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
