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
ORDER_RESPONSE_MESSAGES: dict[int, tuple[str, ...]] = {
    1: (
        "Fine, {item}. Barely deserved, but Melody will allow it.",
        "Here is your {item}. Try earning it properly next time.",
        "Melody is handing over the {item}, but the hour count is tragic.",
        "Take the {item}. This is charity, not service.",
        "Your {item} is approved. Your grind is not.",
    ),
    2: (
        "Melody will get your {item}, but do not look too proud.",
        "Here is the {item}. The effort behind it is still thin.",
        "Approved: one {item}. Consider this a warning sip.",
        "You can have the {item}. The hours need serious work.",
        "Melody says yes to {item}, but only barely.",
    ),
    3: (
        "Your {item} is coming. The training still needs volume.",
        "Melody accepts the {item} order, with reservations.",
        "One {item}, approved. You are not out of the danger zone.",
        "You may have {item}. Add more hours before asking again.",
        "Melody will serve the {item}. Do not waste the mercy.",
    ),
    4: (
        "Melody approves your {item}. Not bad, not impressive.",
        "Here is your {item}. Keep building the hours.",
        "Your {item} is accepted. The progress is visible now.",
        "Melody says yes to {item}. Keep moving.",
        "One {item}, granted. You are warming up.",
    ),
    5: (
        "Your {item} order is approved. Decent work.",
        "Melody has your {item}. The hours are becoming respectable.",
        "Here is the {item}. Keep this pace up.",
        "Melody says yes to {item}. You earned a normal amount of kindness.",
        "One {item}, coming up. Solid effort.",
    ),
    6: (
        "Melody gladly brings your {item}. Nice work this week.",
        "Your {item} is approved. The hours are looking healthy.",
        "Here is your {item}. Keep stacking those sessions.",
        "Melody says yes to {item}. Good momentum.",
        "One {item}, served with approval. You are doing well.",
    ),
    7: (
        "Melody is happy to bring your {item}. Strong week.",
        "Your {item} is on the way. The effort shows.",
        "Here is your {item}. You have been putting in real time.",
        "Melody says yes to {item}. Keep that rhythm.",
        "One {item}, proudly approved. Very solid work.",
    ),
    8: (
        "Melody would love to bring your {item}. Excellent hours.",
        "Your {item} is ready. You have earned the good treatment.",
        "Here is your {item}. Melody noticed the grind.",
        "Melody says yes to {item} with a smile. Great work.",
        "One {item}, happily served. You are carrying the week well.",
    ),
    9: (
        "Melody is delighted to bring your {item}. You earned this.",
        "Your {item} is served with pride. Outstanding effort.",
        "Here is your {item}. Melody is genuinely impressed.",
        "Melody says yes to {item}. That weekly total is beautiful.",
        "One {item}, warmly approved. You have been working hard.",
    ),
    10: (
        "Of course, sweetheart. Melody made your {item} with extra care.",
        "Absolutely. Your {item} is here, and Melody is proud of you.",
        "Yes, gladly. Melody brought your {item} just the way you deserve.",
        "Your {item} is ready. Melody is being very gentle with you today.",
        "Melody would never say no to you right now. Here is your {item}.",
    ),
}


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


def order_message_level(hours: float, *, force_cutest: bool = False) -> int:
    if force_cutest:
        return 10
    bounded_hours = max(0.0, min(hours, ALWAYS_YES_HOURS))
    return min(10, int((bounded_hours / ALWAYS_YES_HOURS) * 9) + 1)


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
        asset_picker: Callable[[list[Path]], Path] = random.choice,
        message_picker: Callable[[tuple[str, ...]], str] = random.choice,
    ) -> None:
        self.bot = bot
        self._repo = repo
        self._assets_root = assets_root
        self._rng = rng
        self._asset_picker = asset_picker
        self._message_picker = message_picker

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

        priority_role = _has_new_asset_priority_role(ctx.author)
        tone_level = order_message_level(hours, force_cutest=priority_role)
        order_text = self._message_picker(ORDER_RESPONSE_MESSAGES[tone_level]).format(item=normalized_item)
        message = (
            f"{order_text}\n"
            f"Tracked time in the {ORDER_WINDOW_LABEL}: **{_format_hours(seconds)}**."
        )
        asset = self._pick_asset(
            normalized_item,
            prefer_user_added=priority_role,
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
                return self._asset_picker(assets)

        assets = self._assets_for_item(item_dir, extensions=ORDER_ASSET_EXTENSIONS)
        if not assets:
            return None
        return self._asset_picker(assets)

    @staticmethod
    def _assets_for_item(item_dir: Path, *, extensions: tuple[str, ...]) -> list[Path]:
        return sorted(
            path
            for path in item_dir.iterdir()
            if path.is_file() and path.suffix.casefold() in extensions
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(OrdersCog(bot, getattr(bot, "user_repo")))
