from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Iterable, Optional
from zoneinfo import ZoneInfo

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


EGYPT_TZ = ZoneInfo("Africa/Cairo")
EGYPT_DAY_START_HOUR = 5
TIMESHEET_MAX_DAYS = 31
MAX_LOOKBACK_DELTA = timedelta(days=30 * 12)


@dataclass(slots=True)
class VoiceTimesheetWindow:
    day_starts: list[datetime]
    since_utc: datetime
    until_utc: datetime
    label: str


@dataclass(slots=True)
class VoiceTimesheetRow:
    discord_id: int
    daily_seconds: list[float]
    total_seconds: float
    max_day_seconds: float


@dataclass(slots=True)
class VoiceTimesheetReport:
    window: VoiceTimesheetWindow
    rows: list[VoiceTimesheetRow]


@dataclass(slots=True)
class VoiceMaxRequest:
    mode: str
    window_delta: timedelta
    window_label: str
    lookback_delta: timedelta
    lookback_label: str
    fixed_day: bool


@dataclass(slots=True)
class VoiceMaxResult:
    discord_id: int
    seconds: float
    start_utc: datetime
    end_utc: datetime


@dataclass(slots=True)
class VoiceMaxReport:
    request: VoiceMaxRequest
    since_utc: datetime
    until_utc: datetime
    results: list[VoiceMaxResult]


class VoiceService:
    MAX_USAGE = (
        "Usage:\n"
        "`max day last <amount> <day/week/month>`\n"
        "`max week last <amount> <week/month>`\n"
        "`max month last <amount> months`\n"
        "`max range <amount> <hour/day/week/month> last <lookback_amount> <hour/day/week/month>`"
    )

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

    @staticmethod
    def _ensure_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _to_egypt(value: datetime) -> datetime:
        return VoiceService._ensure_utc(value).astimezone(EGYPT_TZ)

    @staticmethod
    def egypt_day_start(value: datetime) -> datetime:
        local = VoiceService._to_egypt(value)
        boundary = local.replace(hour=EGYPT_DAY_START_HOUR, minute=0, second=0, microsecond=0)
        if local < boundary:
            boundary -= timedelta(days=1)
        return boundary

    @staticmethod
    def _parse_positive_int(raw: str, label: str) -> int:
        try:
            amount = int(raw)
        except ValueError as exc:
            raise ValueError(f"`{label}` must be a positive integer.") from exc
        if amount <= 0:
            raise ValueError(f"`{label}` must be greater than 0.")
        return amount

    @staticmethod
    def _plural(amount: int, unit: str) -> str:
        return f"{amount} {unit}{'' if amount == 1 else 's'}"

    def parse_timesheet_days(self, tokens: tuple[str, ...]) -> int:
        if len(tokens) != 3 or tokens[0].strip().lower() != "last":
            raise ValueError("Usage: `last <days> days`.")

        days = self._parse_positive_int(tokens[1], "days")
        unit = self._normalize_window_unit(tokens[2])
        if unit != "day":
            raise ValueError("Timesheet unit must be `day` or `days`.")
        if days > TIMESHEET_MAX_DAYS:
            raise ValueError(f"Timesheet range is capped at {TIMESHEET_MAX_DAYS} days.")
        return days

    def timesheet_window(self, *, now: datetime, days: int) -> VoiceTimesheetWindow:
        if days <= 0:
            raise ValueError("`days` must be greater than 0.")
        if days > TIMESHEET_MAX_DAYS:
            raise ValueError(f"Timesheet range is capped at {TIMESHEET_MAX_DAYS} days.")

        until_utc = self._ensure_utc(now)
        current_day_start = self.egypt_day_start(until_utc)
        first_day_start = current_day_start - timedelta(days=days - 1)
        day_starts = [first_day_start + timedelta(days=index) for index in range(days)]
        return VoiceTimesheetWindow(
            day_starts=day_starts,
            since_utc=day_starts[0].astimezone(UTC),
            until_utc=until_utc,
            label=f"last {days} day{'' if days == 1 else 's'}",
        )

    def parse_max_tokens(self, tokens: tuple[str, ...]) -> VoiceMaxRequest:
        if not tokens:
            raise ValueError(self.MAX_USAGE)

        mode = tokens[0].strip().lower()
        if mode in {"day", "week", "month"}:
            if len(tokens) != 4 or tokens[1].strip().lower() != "last":
                raise ValueError(self.MAX_USAGE)
            lookback_amount = self._parse_positive_int(tokens[2], "amount")
            lookback_unit = self._normalize_window_unit(tokens[3])
            if mode == "day":
                allowed_units = {"day", "week", "month"}
            elif mode == "week":
                allowed_units = {"week", "month"}
            else:
                allowed_units = {"month"}
            if lookback_unit not in allowed_units:
                allowed_text = "/".join(sorted(allowed_units))
                raise ValueError(f"`max {mode}` lookback unit must be one of: `{allowed_text}`.")

            lookback_delta = self._window_delta(lookback_amount, lookback_unit)
            self._validate_max_lookback(lookback_delta)
            if mode == "day":
                window_delta = timedelta(days=1)
                window_label = "day"
            elif mode == "week":
                window_delta = timedelta(days=7)
                window_label = "7 days"
            else:
                window_delta = timedelta(days=30)
                window_label = "30 days"
            return VoiceMaxRequest(
                mode=mode,
                window_delta=window_delta,
                window_label=window_label,
                lookback_delta=lookback_delta,
                lookback_label=f"last {self._plural(lookback_amount, lookback_unit)}",
                fixed_day=mode == "day",
            )

        if mode == "range":
            if len(tokens) != 6 or tokens[3].strip().lower() != "last":
                raise ValueError(self.MAX_USAGE)
            window_amount = self._parse_positive_int(tokens[1], "amount")
            window_unit = self._normalize_window_unit(tokens[2])
            if window_unit is None:
                raise ValueError("Range unit must be one of: `hour`, `day`, `week`, `month`.")
            lookback_amount = self._parse_positive_int(tokens[4], "lookback_amount")
            lookback_unit = self._normalize_window_unit(tokens[5])
            if lookback_unit is None:
                raise ValueError("Lookback unit must be one of: `hour`, `day`, `week`, `month`.")

            lookback_delta = self._window_delta(lookback_amount, lookback_unit)
            self._validate_max_lookback(lookback_delta)
            return VoiceMaxRequest(
                mode=mode,
                window_delta=self._window_delta(window_amount, window_unit),
                window_label=self._plural(window_amount, window_unit),
                lookback_delta=lookback_delta,
                lookback_label=f"last {self._plural(lookback_amount, lookback_unit)}",
                fixed_day=False,
            )

        raise ValueError(self.MAX_USAGE)

    @staticmethod
    def _validate_max_lookback(delta: timedelta) -> None:
        if delta > MAX_LOOKBACK_DELTA:
            raise ValueError("Max report lookback is capped at 12 months.")

    @staticmethod
    def _row_value(row: Any, key: str) -> Any:
        if isinstance(row, dict):
            return row[key]
        return getattr(row, key)

    def _coerce_interval(self, row: Any) -> tuple[int, datetime, datetime] | None:
        try:
            discord_id = int(self._row_value(row, "discord_id"))
            start = self._row_value(row, "start_ts")
            end = self._row_value(row, "end_ts")
        except (AttributeError, KeyError, TypeError, ValueError):
            return None
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            return None
        start_utc = self._ensure_utc(start)
        end_utc = self._ensure_utc(end)
        if end_utc <= start_utc:
            return None
        return discord_id, start_utc, end_utc

    def merge_intervals_by_user(
        self,
        intervals: Iterable[Any],
        *,
        since_utc: datetime,
        until_utc: datetime,
    ) -> dict[int, list[tuple[datetime, datetime]]]:
        since = self._ensure_utc(since_utc)
        until = self._ensure_utc(until_utc)
        raw_by_user: dict[int, list[tuple[datetime, datetime]]] = {}
        for interval in intervals:
            coerced = self._coerce_interval(interval)
            if coerced is None:
                continue
            discord_id, start, end = coerced
            clipped_start = max(start, since)
            clipped_end = min(end, until)
            if clipped_end <= clipped_start:
                continue
            raw_by_user.setdefault(discord_id, []).append((clipped_start, clipped_end))

        merged_by_user: dict[int, list[tuple[datetime, datetime]]] = {}
        for discord_id, user_intervals in raw_by_user.items():
            user_intervals.sort(key=lambda interval: interval[0])
            merged: list[tuple[datetime, datetime]] = []
            current_start, current_end = user_intervals[0]
            for start, end in user_intervals[1:]:
                if start <= current_end:
                    if end > current_end:
                        current_end = end
                    continue
                merged.append((current_start, current_end))
                current_start, current_end = start, end
            merged.append((current_start, current_end))
            merged_by_user[discord_id] = merged
        return merged_by_user

    @staticmethod
    def _overlap_seconds(
        intervals: Iterable[tuple[datetime, datetime]],
        start: datetime,
        end: datetime,
    ) -> float:
        total = 0.0
        for interval_start, interval_end in intervals:
            if interval_end <= start:
                continue
            if interval_start >= end:
                break
            overlap_start = max(interval_start, start)
            overlap_end = min(interval_end, end)
            if overlap_end > overlap_start:
                total += (overlap_end - overlap_start).total_seconds()
        return total

    def build_timesheet_report(
        self,
        *,
        intervals: Iterable[Any],
        user_ids: Iterable[int],
        now: datetime,
        days: int,
    ) -> VoiceTimesheetReport:
        window = self.timesheet_window(now=now, days=days)
        merged_by_user = self.merge_intervals_by_user(
            intervals,
            since_utc=window.since_utc,
            until_utc=window.until_utc,
        )

        bucket_ranges = [
            (
                day_start.astimezone(UTC),
                min((day_start + timedelta(days=1)).astimezone(UTC), window.until_utc),
            )
            for day_start in window.day_starts
        ]
        rows: list[VoiceTimesheetRow] = []
        for raw_user_id in user_ids:
            user_id = int(raw_user_id)
            user_intervals = merged_by_user.get(user_id, [])
            daily_seconds = [
                self._overlap_seconds(user_intervals, bucket_start, bucket_end)
                for bucket_start, bucket_end in bucket_ranges
            ]
            rows.append(
                VoiceTimesheetRow(
                    discord_id=user_id,
                    daily_seconds=daily_seconds,
                    total_seconds=sum(daily_seconds),
                    max_day_seconds=max(daily_seconds, default=0.0),
                )
            )
        rows.sort(key=lambda row: row.total_seconds, reverse=True)
        return VoiceTimesheetReport(window=window, rows=rows)

    def max_lookback_window(self, *, now: datetime, request: VoiceMaxRequest) -> tuple[datetime, datetime]:
        until_utc = self._ensure_utc(now)
        since_local = self.egypt_day_start(until_utc) - request.lookback_delta
        return since_local.astimezone(UTC), until_utc

    def build_max_report(
        self,
        *,
        intervals: Iterable[Any],
        user_ids: Iterable[int],
        now: datetime,
        request: VoiceMaxRequest,
    ) -> VoiceMaxReport:
        since_utc, until_utc = self.max_lookback_window(now=now, request=request)
        merged_by_user = self.merge_intervals_by_user(
            intervals,
            since_utc=since_utc,
            until_utc=until_utc,
        )

        results: list[VoiceMaxResult] = []
        since_local = since_utc.astimezone(EGYPT_TZ)
        for raw_user_id in user_ids:
            user_id = int(raw_user_id)
            user_intervals = merged_by_user.get(user_id, [])
            if request.fixed_day:
                seconds, start_utc, end_utc = self._best_fixed_egypt_day(
                    user_intervals,
                    since_local=since_local,
                    until_utc=until_utc,
                )
            else:
                seconds, start_utc, end_utc = self._best_rolling_window(
                    user_intervals,
                    since_utc=since_utc,
                    until_utc=until_utc,
                    duration=request.window_delta,
                )
            results.append(
                VoiceMaxResult(
                    discord_id=user_id,
                    seconds=seconds,
                    start_utc=start_utc,
                    end_utc=end_utc,
                )
            )

        results.sort(key=lambda row: row.seconds, reverse=True)
        return VoiceMaxReport(request=request, since_utc=since_utc, until_utc=until_utc, results=results)

    def _best_fixed_egypt_day(
        self,
        intervals: list[tuple[datetime, datetime]],
        *,
        since_local: datetime,
        until_utc: datetime,
    ) -> tuple[float, datetime, datetime]:
        bucket_start_local = since_local.replace(hour=EGYPT_DAY_START_HOUR, minute=0, second=0, microsecond=0)
        if bucket_start_local > since_local:
            bucket_start_local -= timedelta(days=1)

        best_seconds = -1.0
        best_start_utc = max(bucket_start_local.astimezone(UTC), since_local.astimezone(UTC))
        best_end_utc = min((bucket_start_local + timedelta(days=1)).astimezone(UTC), until_utc)
        while bucket_start_local.astimezone(UTC) < until_utc:
            next_start_local = bucket_start_local + timedelta(days=1)
            bucket_start_utc = max(bucket_start_local.astimezone(UTC), since_local.astimezone(UTC))
            bucket_end_utc = min(next_start_local.astimezone(UTC), until_utc)
            if bucket_end_utc > bucket_start_utc:
                seconds = self._overlap_seconds(intervals, bucket_start_utc, bucket_end_utc)
                if seconds > best_seconds:
                    best_seconds = seconds
                    best_start_utc = bucket_start_utc
                    best_end_utc = bucket_end_utc
            bucket_start_local = next_start_local

        return max(0.0, best_seconds), best_start_utc, best_end_utc

    def _best_rolling_window(
        self,
        intervals: list[tuple[datetime, datetime]],
        *,
        since_utc: datetime,
        until_utc: datetime,
        duration: timedelta,
    ) -> tuple[float, datetime, datetime]:
        if duration <= timedelta(0) or until_utc <= since_utc:
            return 0.0, since_utc, until_utc

        latest_start = until_utc - duration
        if latest_start <= since_utc:
            return self._overlap_seconds(intervals, since_utc, until_utc), since_utc, until_utc

        def clamp_start(candidate: datetime) -> datetime:
            if candidate < since_utc:
                return since_utc
            if candidate > latest_start:
                return latest_start
            return candidate

        candidates = {since_utc, latest_start}
        for start, end in intervals:
            candidates.add(clamp_start(start))
            candidates.add(clamp_start(end))
            candidates.add(clamp_start(start - duration))
            candidates.add(clamp_start(end - duration))

        best_seconds = -1.0
        best_start = since_utc
        for candidate_start in sorted(candidates):
            candidate_end = candidate_start + duration
            seconds = self._overlap_seconds(intervals, candidate_start, candidate_end)
            if seconds > best_seconds + 1e-9 or (
                abs(seconds - best_seconds) <= 1e-9 and candidate_start > best_start
            ):
                best_seconds = seconds
                best_start = candidate_start
        return max(0.0, best_seconds), best_start, best_start + duration

    @staticmethod
    def _hours_cell(seconds: float, *, width: int = 5) -> str:
        return f"{seconds / 3600:>{width}.1f}"

    @staticmethod
    def _label_for(discord_id: int, label_lookup: Callable[[int], Optional[str]]) -> str:
        label = label_lookup(discord_id) or str(discord_id)
        return " ".join(str(label).split())

    def timesheet_lines(
        self,
        *,
        report: VoiceTimesheetReport,
        label_lookup: Callable[[int], Optional[str]],
    ) -> list[str]:
        day_labels = [day_start.strftime("%m/%d") for day_start in report.window.day_starts]
        lines = [f"{'user':<18.18} {' '.join(day_labels)} total   max"]
        for row in report.rows:
            label = self._label_for(row.discord_id, label_lookup)
            day_cells = " ".join(self._hours_cell(seconds) for seconds in row.daily_seconds)
            lines.append(
                f"{label:<18.18} {day_cells} "
                f"{self._hours_cell(row.total_seconds, width=6)} {self._hours_cell(row.max_day_seconds)}"
            )
        return lines

    def max_report_lines(
        self,
        *,
        report: VoiceMaxReport,
        label_lookup: Callable[[int], Optional[str]],
    ) -> list[str]:
        lines = [f"{'rk':<4} {'user':<18.18} {'max':>8} {'start (EGY)':<14} {'end (EGY)':<14}"]
        for index, row in enumerate(report.results, start=1):
            label = self._label_for(row.discord_id, label_lookup)
            start_local = row.start_utc.astimezone(EGYPT_TZ).strftime("%m/%d %H:%M")
            end_local = row.end_utc.astimezone(EGYPT_TZ).strftime("%m/%d %H:%M")
            lines.append(
                f"{self.rank_prefix(index):<4} {label:<18.18} "
                f"{self.hours(row.seconds):>8} {start_local:<14} {end_local:<14}"
            )
        return lines

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
