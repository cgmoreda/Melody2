from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from services.discord_output import DISCORD_MESSAGE_CHAR_LIMIT
from services.voice_service import VoiceService


def test_parse_window_tokens_aliases() -> None:
    service = VoiceService()
    now = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)

    since, label = service.parse_window_tokens(now=now, tokens=("hrs",))
    assert label == "last 1 hour"
    assert since == now - timedelta(hours=1)

    since2, label2 = service.parse_window_tokens(now=now, tokens=("last", "2", "weeks"))
    assert label2 == "last 2 weeks"
    assert since2 == now - timedelta(weeks=2)


def test_parse_window_tokens_invalid_usage() -> None:
    service = VoiceService()
    now = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
    with pytest.raises(ValueError):
        service.parse_window_tokens(now=now, tokens=("last", "-1", "day"))


def test_render_ranked_message_is_capped() -> None:
    lines = [f"#{index:03d} {'user' * 12} 1234.56h" for index in range(1, 300)]
    rendered = VoiceService.render_ranked_message(
        title="**Solo Voice Hours (all time)**",
        lines=lines,
        max_lines=250,
        overflow_label="users",
    )

    assert len(rendered) <= DISCORD_MESSAGE_CHAR_LIMIT
    assert rendered.count("```") % 2 == 0


def test_leaderboard_lines_uses_fallback_display_name() -> None:
    service = VoiceService()
    totals = {10: 7200.0, 20: 3600.0}
    handles = {10: "handle10"}

    lines = service.leaderboard_lines(
        totals=totals,
        handle_by_discord_id=handles,
        display_name_lookup=lambda discord_id: f"user-{discord_id}",
    )

    assert lines[0] == "rk   handle             hours"
    assert "handle10" in lines[1]
    assert "user-20" in lines[2]


def test_timesheet_splits_sessions_on_cairo_5am_boundary() -> None:
    service = VoiceService()
    cairo = ZoneInfo("Africa/Cairo")
    now = datetime(2026, 1, 10, 7, 0, tzinfo=cairo).astimezone(UTC)
    intervals = [
        {
            "discord_id": 10,
            "start_ts": datetime(2026, 1, 10, 4, 30, tzinfo=cairo).astimezone(UTC),
            "end_ts": datetime(2026, 1, 10, 6, 30, tzinfo=cairo).astimezone(UTC),
        }
    ]

    report = service.build_timesheet_report(intervals=intervals, user_ids=[10], now=now, days=2)

    row = report.rows[0]
    assert [day_start.strftime("%Y-%m-%d %H:%M") for day_start in report.window.day_starts] == [
        "2026-01-09 05:00",
        "2026-01-10 05:00",
    ]
    assert row.daily_seconds == [30 * 60.0, 90 * 60.0]
    assert row.total_seconds == 2 * 3600.0


def test_timesheet_merges_overlaps_and_keeps_zero_hour_users() -> None:
    service = VoiceService()
    cairo = ZoneInfo("Africa/Cairo")
    now = datetime(2026, 1, 10, 9, 0, tzinfo=cairo).astimezone(UTC)
    intervals = [
        {
            "discord_id": 10,
            "start_ts": datetime(2026, 1, 10, 5, 0, tzinfo=cairo).astimezone(UTC),
            "end_ts": datetime(2026, 1, 10, 7, 0, tzinfo=cairo).astimezone(UTC),
        },
        {
            "discord_id": 10,
            "start_ts": datetime(2026, 1, 10, 6, 0, tzinfo=cairo).astimezone(UTC),
            "end_ts": datetime(2026, 1, 10, 8, 0, tzinfo=cairo).astimezone(UTC),
        },
    ]

    report = service.build_timesheet_report(intervals=intervals, user_ids=[10, 20], now=now, days=1)
    rows = {row.discord_id: row for row in report.rows}

    assert rows[10].total_seconds == 3 * 3600.0
    assert rows[20].total_seconds == 0.0
    assert rows[20].daily_seconds == [0.0]


def test_max_day_selects_best_fixed_egypt_day_bucket() -> None:
    service = VoiceService()
    cairo = ZoneInfo("Africa/Cairo")
    now = datetime(2026, 1, 10, 12, 0, tzinfo=cairo).astimezone(UTC)
    request = service.parse_max_tokens(("day", "last", "1", "week"))
    intervals = [
        {
            "discord_id": 10,
            "start_ts": datetime(2026, 1, 7, 5, 0, tzinfo=cairo).astimezone(UTC),
            "end_ts": datetime(2026, 1, 7, 8, 0, tzinfo=cairo).astimezone(UTC),
        },
        {
            "discord_id": 10,
            "start_ts": datetime(2026, 1, 9, 5, 0, tzinfo=cairo).astimezone(UTC),
            "end_ts": datetime(2026, 1, 9, 10, 0, tzinfo=cairo).astimezone(UTC),
        },
    ]

    report = service.build_max_report(intervals=intervals, user_ids=[10], now=now, request=request)
    result = report.results[0]

    assert result.seconds == 5 * 3600.0
    assert result.start_utc.astimezone(cairo).strftime("%Y-%m-%d %H:%M") == "2026-01-09 05:00"
    assert result.end_utc.astimezone(cairo).strftime("%Y-%m-%d %H:%M") == "2026-01-10 05:00"


def test_max_week_selects_best_rolling_7_day_window() -> None:
    service = VoiceService()
    cairo = ZoneInfo("Africa/Cairo")
    now = datetime(2026, 1, 20, 12, 0, tzinfo=cairo).astimezone(UTC)
    request = service.parse_max_tokens(("week", "last", "1", "month"))
    intervals = [
        {
            "discord_id": 10,
            "start_ts": datetime(2026, 1, 1, 5, 0, tzinfo=cairo).astimezone(UTC),
            "end_ts": datetime(2026, 1, 1, 7, 0, tzinfo=cairo).astimezone(UTC),
        },
        {
            "discord_id": 10,
            "start_ts": datetime(2026, 1, 10, 5, 0, tzinfo=cairo).astimezone(UTC),
            "end_ts": datetime(2026, 1, 10, 10, 0, tzinfo=cairo).astimezone(UTC),
        },
        {
            "discord_id": 10,
            "start_ts": datetime(2026, 1, 12, 5, 0, tzinfo=cairo).astimezone(UTC),
            "end_ts": datetime(2026, 1, 12, 9, 0, tzinfo=cairo).astimezone(UTC),
        },
    ]

    report = service.build_max_report(intervals=intervals, user_ids=[10], now=now, request=request)
    result = report.results[0]

    assert result.seconds == 9 * 3600.0
    assert result.start_utc.astimezone(cairo).strftime("%Y-%m-%d %H:%M") == "2026-01-10 05:00"
    assert result.end_utc.astimezone(cairo).strftime("%Y-%m-%d %H:%M") == "2026-01-17 05:00"


def test_max_range_selects_best_custom_rolling_window() -> None:
    service = VoiceService()
    cairo = ZoneInfo("Africa/Cairo")
    now = datetime(2026, 1, 10, 12, 0, tzinfo=cairo).astimezone(UTC)
    request = service.parse_max_tokens(("range", "2", "hours", "last", "1", "week"))
    intervals = [
        {
            "discord_id": 10,
            "start_ts": datetime(2026, 1, 10, 5, 0, tzinfo=cairo).astimezone(UTC),
            "end_ts": datetime(2026, 1, 10, 6, 0, tzinfo=cairo).astimezone(UTC),
        },
        {
            "discord_id": 10,
            "start_ts": datetime(2026, 1, 10, 6, 30, tzinfo=cairo).astimezone(UTC),
            "end_ts": datetime(2026, 1, 10, 8, 30, tzinfo=cairo).astimezone(UTC),
        },
        {
            "discord_id": 10,
            "start_ts": datetime(2026, 1, 10, 8, 45, tzinfo=cairo).astimezone(UTC),
            "end_ts": datetime(2026, 1, 10, 9, 15, tzinfo=cairo).astimezone(UTC),
        },
    ]

    report = service.build_max_report(intervals=intervals, user_ids=[10], now=now, request=request)
    result = report.results[0]

    assert result.seconds == 2 * 3600.0
    assert result.start_utc.astimezone(cairo).strftime("%Y-%m-%d %H:%M") == "2026-01-10 06:30"
    assert result.end_utc.astimezone(cairo).strftime("%Y-%m-%d %H:%M") == "2026-01-10 08:30"
