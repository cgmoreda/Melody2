from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
