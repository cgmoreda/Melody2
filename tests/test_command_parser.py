from __future__ import annotations

import pytest

from services.command_parser import (
    CommandParseError,
    extract_single_clause,
    join_free_text,
    parse_positive_float,
    parse_positive_int,
    parse_single_value_clause,
    require_no_tokens,
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
