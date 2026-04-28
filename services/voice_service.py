from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Callable, Optional

from services.discord_output import DISCORD_MESSAGE_CHAR_LIMIT, clip_text

SCOLDING_TIERS: tuple[tuple[str, ...], ...] = (
    (
        "Warm-up arc only. Time to lock in.",
        "The grind is waiting for you. Start now.",
        "You are behind schedule. Catch up today.",
    ),
    (
        "Training Arc badge, trainee output.",
        "This is not enough volume. Push harder.",
        "You owe this role more hours than this.",
    ),
    (
        "That timer is allergic to your presence.",
        "Your solo hours are missing in action.",
        "This effort level does not survive rank-up season.",
    ),
    (
        "Production is critically low. Immediate grind required.",
        "At this pace, even your excuses are underperforming.",
        "You are speedrunning the spectator route.",
    ),
)


class VoiceService:
    @staticmethod
    def hours(seconds: float) -> str:
        return f"{seconds / 3600:.2f}h"

    @staticmethod
    def rank_prefix(rank: int) -> str:
        return f"#{rank}"

    @staticmethod
    def is_solo_channel_name(channel_name: str) -> bool:
        normalized = channel_name.strip().lower()
        return normalized.startswith(("solo #", "solo room"))

    @staticmethod
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

    @staticmethod
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

    def parse_window_tokens(
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
            normalized = self._normalize_window_unit(short)
            if normalized is not None:
                return now - self._window_delta(1, normalized), f"last 1 {normalized}"
            raise ValueError("Usage: `... [last <x> <hour/day/week/month>]`")

        if len(tokens) == 3 and tokens[0].strip().lower() == "last":
            try:
                amount = int(tokens[1])
            except ValueError as exc:
                raise ValueError("`x` must be a positive integer.") from exc
            if amount <= 0:
                raise ValueError("`x` must be greater than 0.")

            normalized_unit = self._normalize_window_unit(tokens[2])
            if normalized_unit is None:
                raise ValueError("Unit must be one of: `hour`, `day`, `week`, `month`.")
            return (
                now - self._window_delta(amount, normalized_unit),
                f"last {amount} {normalized_unit}{'' if amount == 1 else 's'}",
            )

        raise ValueError("Usage: `... [last <x> <hour/day/week/month>]`")

    @staticmethod
    def sorted_totals(totals: dict[int, float]) -> list[tuple[int, float]]:
        return sorted(totals.items(), key=lambda item: item[1], reverse=True)

    def leaderboard_lines(
        self,
        *,
        totals: dict[int, float],
        handle_by_discord_id: dict[int, str],
        display_name_lookup: Callable[[int], Optional[str]],
    ) -> list[str]:
        lines: list[str] = ["rk   handle             hours"]
        for index, (discord_id, seconds) in enumerate(self.sorted_totals(totals), start=1):
            handle = handle_by_discord_id.get(discord_id)
            if handle is None:
                handle = display_name_lookup(discord_id) or str(discord_id)
            lines.append(f"{self.rank_prefix(index):<4} {handle:<18.18} {self.hours(seconds)}")
        return lines

    @staticmethod
    def render_ranked_message(*, title: str, lines: list[str], max_lines: int, overflow_label: str) -> str:
        visible_limit = min(len(lines), max_lines + 1)

        while visible_limit > 0:
            shown = lines[:visible_limit]
            parts = [title, "```text", *shown, "```"]
            remaining_count = max(0, len(lines) - visible_limit)
            if remaining_count > 0:
                parts.append(f"... and {remaining_count} more {overflow_label}")
            rendered = "\n".join(parts)
            if len(rendered) <= DISCORD_MESSAGE_CHAR_LIMIT:
                return rendered
            visible_limit -= 1

        return clip_text(f"{title}\n```text\n```", limit=DISCORD_MESSAGE_CHAR_LIMIT)

    @staticmethod
    def find_rank(totals: dict[int, float], discord_id: int) -> Optional[tuple[int, float]]:
        if discord_id not in totals:
            return None
        ordered = sorted(totals.items(), key=lambda item: item[1], reverse=True)
        for rank, (candidate_id, seconds) in enumerate(ordered, start=1):
            if candidate_id == discord_id:
                return rank, seconds
        return None

    @staticmethod
    def severity_index(*, minimum_hours: float, worked_hours: float) -> int:
        if minimum_hours <= 0:
            return 0
        shortfall_ratio = max(0.0, (minimum_hours - worked_hours) / minimum_hours)
        max_idx = len(SCOLDING_TIERS) - 1
        if shortfall_ratio < 0.20:
            return 0
        if shortfall_ratio < 0.45:
            return min(1, max_idx)
        if shortfall_ratio < 0.75:
            return min(2, max_idx)
        return max_idx

    @staticmethod
    def pick_scolding(*, minimum_hours: float, worked_hours: float) -> str:
        severity = VoiceService.severity_index(minimum_hours=minimum_hours, worked_hours=worked_hours)
        candidate_tiers = SCOLDING_TIERS[: severity + 1]
        weights = [float(index + 1) for index in range(len(candidate_tiers))]
        chosen_tier = random.choices(candidate_tiers, weights=weights, k=1)[0]
        return random.choice(chosen_tier)
