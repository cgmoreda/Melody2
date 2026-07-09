from __future__ import annotations

import pytest

from datetime import timedelta

from services.command_parser import (
    CommandParseError,
    extract_single_clause,
    join_free_text,
    parse_positive_float,
    parse_positive_int,
    parse_single_value_clause,
    require_no_tokens,
    parse_timeout_duration,
)


def test_extract_single_clause_preserves_other_tokens_in_order() -> None:
    clause, remaining = extract_single_clause(
        ("user", "@member", "last", "2", "weeks"),
        {"last"},
        3,
        name="last",
    )

    assert clause is not None
    assert clause.tokens == ("last", "2", "weeks")
    assert remaining == ("user", "@member")


def test_extract_single_clause_rejects_duplicates() -> None:
    with pytest.raises(CommandParseError, match="Only one `last` clause is allowed"):
        extract_single_clause(
            ("last", "1", "week", "last", "2", "weeks"),
            {"last"},
            3,
            name="last",
        )


def test_require_no_tokens_rejects_unknown_leftovers() -> None:
    with pytest.raises(CommandParseError, match="Usage"):
        require_no_tokens(("wat",), usage="Usage: command")


def test_parse_positive_int_and_float_validate_values() -> None:
    assert parse_positive_int("3", "limit") == 3
    assert parse_positive_float("2.5", "x") == pytest.approx(2.5)

    with pytest.raises(CommandParseError, match="positive integer"):
        parse_positive_int("x", "limit")
    with pytest.raises(CommandParseError, match="greater than 0"):
        parse_positive_float("0", "x")


def test_parse_single_value_clause_extracts_in_multiple_orders() -> None:
    value, remaining = parse_single_value_clause(
        ("last", "1", "week", "top", "5"),
        {"top"},
        name="top",
        parser=lambda raw: parse_positive_int(raw, "limit"),
    )

    assert value == 5
    assert remaining == ("last", "1", "week")


def test_join_free_text_preserves_message_words() -> None:
    assert join_free_text(("Please", "update", "sheets")) == "Please update sheets"


def test_parse_timeout_duration_valid_separated() -> None:
    delta, label = parse_timeout_duration("15", "min")
    assert delta == timedelta(minutes=15)
    assert label == "15 minutes"

    delta, label = parse_timeout_duration("2.5", "hours")
    assert delta == timedelta(hours=2.5)
    assert label == "2.5 hours"

    delta, label = parse_timeout_duration("1", "d")
    assert delta == timedelta(days=1)
    assert label == "1 day"


def test_parse_timeout_duration_valid_combined() -> None:
    delta, label = parse_timeout_duration("15m")
    assert delta == timedelta(minutes=15)
    assert label == "15 minutes"

    delta, label = parse_timeout_duration("30.5h")
    assert delta == timedelta(hours=30.5)
    assert label == "30.5 hours"


def test_parse_timeout_duration_invalid_format() -> None:
    with pytest.raises(CommandParseError, match="Invalid duration format"):
        parse_timeout_duration("xm")


def test_parse_timeout_duration_invalid_value() -> None:
    with pytest.raises(CommandParseError, match="strictly positive"):
        parse_timeout_duration("0", "m")
    with pytest.raises(CommandParseError, match="strictly positive"):
        parse_timeout_duration("-5", "m")
    with pytest.raises(CommandParseError, match="strictly positive"):
        parse_timeout_duration("-5m")


def test_parse_timeout_duration_invalid_unit() -> None:
    with pytest.raises(CommandParseError, match="Invalid duration unit"):
        parse_timeout_duration("15", "x")


def test_parse_timeout_duration_exceeds_limit() -> None:
    with pytest.raises(CommandParseError, match="cannot exceed 28 days"):
        parse_timeout_duration("30", "d")


def test_parse_timeout_duration_case_insensitive() -> None:
    delta, label = parse_timeout_duration("15", "MIN")
    assert delta == timedelta(minutes=15)
    assert label == "15 minutes"

    delta, label = parse_timeout_duration("15M")
    assert delta == timedelta(minutes=15)
    assert label == "15 minutes"
