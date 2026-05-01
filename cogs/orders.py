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
        "Melody slides the {item} over like evidence in a trial.",
        "Here is the {item}. The cup did more work than you did.",
        "Melody found a {item} in the back and decided mercy is cheaper than arguing.",
        "Take the {item}. Do not make eye contact with the scoreboard.",
        "Your {item} has been released on probation.",
    ),
    2: (
        "Melody grants one {item}. The receipt says try harder.",
        "Here is the {item}. It arrived faster than your training arc.",
        "One {item}, poured with a raised eyebrow.",
        "Melody accepts the {item} order and files it under suspicious optimism.",
        "Your {item} is ready. The standards committee is watching.",
    ),
    3: (
        "Your {item} is coming. Melody added a tiny clock-shaped warning label.",
        "Melody approves the {item}, but the spoon is tapping impatiently.",
        "One {item}, served with the energy of a teacher extending a deadline.",
        "You may have the {item}. The next one expects a better story.",
        "Melody sends the {item} out with a note: almost counts, barely.",
    ),
    4: (
        "Melody brings your {item}. The tray is no longer judging loudly.",
        "Here is the {item}. The week is starting to look alive.",
        "Your {item} is approved. Melody only squinted once.",
        "Melody sends the {item}. Keep feeding the timer.",
        "One {item}, granted. The engine is finally making noise.",
    ),
    5: (
        "Your {item} is approved. Melody put it on the regular tray.",
        "Melody has your {item}. The timer did not embarrass you today.",
        "Here is the {item}. This is what acceptable looks like.",
        "Melody says yes to {item}. The ledger balances for now.",
        "One {item}, coming up. The week has a pulse.",
    ),
    6: (
        "Melody brings your {item} with the good cup.",
        "Your {item} is approved. The hours are starting to sparkle.",
        "Here is the {item}. Keep stacking the quiet wins.",
        "Melody says yes to {item}. The rhythm is working.",
        "One {item}, served before the kettle even complains.",
    ),
    7: (
        "Melody sends your {item} with a little victory stamp.",
        "Your {item} is on the way. The week left fingerprints.",
        "Here is the {item}. The timer speaks well of you.",
        "Melody says yes to {item}. Keep marching like that.",
        "One {item}, polished and ready. The effort is obvious.",
    ),
    8: (
        "Melody brings your {item} like it belongs on a silver tray.",
        "Your {item} is ready. The week has been properly conquered.",
        "Here is the {item}. Melody saved you a better seat.",
        "Melody says yes to {item}; the room gets brighter about it.",
        "One {item}, served with the premium napkin.",
    ),
    9: (
        "Melody brings your {item} like a crown in a cup.",
        "Your {item} is ready. The timer practically applauded.",
        "Here is the {item}. Melody saved the best pour for you.",
        "Melody says yes to {item}; even the tray looks proud.",
        "One {item}, served with festival-level respect.",
    ),
    10: (
        "Of course, sweetheart. Melody saved the prettiest {item} for you.",
        "Absolutely. Your {item} is here, and Melody tucked a tiny star beside it.",
        "Yes, gladly. Melody brought your {item} before the cup could miss you.",
        "Your {item} is ready. Melody kept it safe like a secret.",
        "Melody could not possibly refuse. Here is your {item}, made with a little sparkle.",
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
