from __future__ import annotations

import logging
import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands

from db.repository import VoiceRepository

logger = logging.getLogger(__name__)

ORDER_ITEMS: tuple[str, ...] = ("tea", "juice", "coffee")
ORDER_ALIASES: dict[str, str] = {
    "tea": "tea",
    "juice": "juice",
    "coffee": "coffee",
    "coffe": "coffee",
}
ORDER_ASSETS_ROOT = Path(__file__).resolve().parents[1] / "assets" / "orders"
ORDER_ASSET_EXTENSIONS: tuple[str, ...] = (".webp", ".png", ".jpg", ".jpeg")
ORDER_USER_ADDED_ASSET_EXTENSIONS: tuple[str, ...] = (".png",)
ORDER_WINDOW = timedelta(days=7)
ORDER_WINDOW_LABEL = "last 7 days"
ALWAYS_YES_HOURS = 21.0
BASE_ACCEPTANCE_PROBABILITY = 0.10


def normalize_order_item(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    return ORDER_ALIASES.get(raw.strip().casefold())


def order_acceptance_probability(item: str, hours: float) -> float:
    if item == "coffee" or hours >= ALWAYS_YES_HOURS:
        return 1.0
    bounded_hours = max(0.0, min(hours, ALWAYS_YES_HOURS))
    return BASE_ACCEPTANCE_PROBABILITY + (bounded_hours / ALWAYS_YES_HOURS) * (
        1.0 - BASE_ACCEPTANCE_PROBABILITY
    )


def _format_hours(seconds: float) -> str:
    return f"{seconds / 3600.0:.2f}h"


def _has_new_asset_priority_role(member: discord.abc.User) -> bool:
    for role in getattr(member, "roles", ()):
        role_name = getattr(role, "name", "").strip().casefold()
        if role_name == "guest" or "coach" in role_name:
            return True
    return False


class OrdersCog(commands.Cog, name="Orders"):
    def __init__(
        self,
        bot: commands.Bot,
        repo: VoiceRepository,
        *,
        assets_root: Path = ORDER_ASSETS_ROOT,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.bot = bot
        self._repo = repo
        self._assets_root = assets_root
        self._rng = rng

    @commands.command(name="order")
    @commands.guild_only()
    async def order(self, ctx: commands.Context, item: Optional[str] = None) -> None:
        """Ask Melody for tea, juice, or coffee."""
        assert ctx.guild is not None

        normalized_item = normalize_order_item(item)
        if normalized_item is None:
            await ctx.send("Usage: `!order <tea|juice|coffee>`")
            return

        now = datetime.now(tz=UTC)
        totals = await self._repo.get_tracked_voice_totals(
            ctx.guild.id,
            now=now,
            since=now - ORDER_WINDOW,
        )
        seconds = totals.get(ctx.author.id, 0.0)
        hours = seconds / 3600.0
        probability = order_acceptance_probability(normalized_item, hours)
        accepted = probability >= 1.0 or self._rng() < probability

        if not accepted:
            await ctx.send(
                f"Melody says no {normalized_item} this time. "
                f"You have **{_format_hours(seconds)}** in the {ORDER_WINDOW_LABEL}; earn the order."
            )
            return

        message = (
            f"Melody accepts your {normalized_item} order. "
            f"Tracked time in the {ORDER_WINDOW_LABEL}: **{_format_hours(seconds)}**."
        )
        asset = self._pick_asset(
            normalized_item,
            prefer_user_added=_has_new_asset_priority_role(ctx.author),
        )
        if asset is None:
            logger.warning("No order assets found for item %s under %s", normalized_item, self._assets_root)
            await ctx.send(f"{message} The photo tray is empty right now.")
            return

        with asset.open("rb") as fp:
            await ctx.send(message, file=discord.File(fp, filename=asset.name))

    def _pick_asset(self, item: str, *, prefer_user_added: bool = False) -> Optional[Path]:
        item_dir = self._assets_root / item
        if not item_dir.is_dir():
            return None
        if prefer_user_added:
            assets = self._assets_for_item(item_dir, extensions=ORDER_USER_ADDED_ASSET_EXTENSIONS)
            if assets:
                return random.choice(assets)

        assets = self._assets_for_item(item_dir, extensions=ORDER_ASSET_EXTENSIONS)
        if not assets:
            return None
        return random.choice(assets)

    @staticmethod
    def _assets_for_item(item_dir: Path, *, extensions: tuple[str, ...]) -> list[Path]:
        return sorted(
            path
            for path in item_dir.iterdir()
            if path.is_file() and path.suffix.casefold() in extensions
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(OrdersCog(bot, getattr(bot, "user_repo")))
