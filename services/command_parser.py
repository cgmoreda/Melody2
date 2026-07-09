from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import re
from typing import Callable, Iterable, Optional, TypeVar


class CommandParseError(ValueError):
    """Raised when command tokens cannot be parsed safely."""


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ParsedClause:
    tokens: tuple[str, ...]
    index: int


def normalize_token(raw: str) -> str:
    return raw.strip().casefold()


def normalized_aliases(values: Iterable[str]) -> set[str]:
    return {normalize_token(value) for value in values}


def matches(raw: str, aliases: Iterable[str]) -> bool:
    return normalize_token(raw) in normalized_aliases(aliases)


def parse_positive_int(raw: str, label: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise CommandParseError(f"`{label}` must be a positive integer.") from exc
    if value <= 0:
        raise CommandParseError(f"`{label}` must be greater than 0.")
    return value


def parse_positive_float(raw: str, label: str, *, number_message: Optional[str] = None) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        if number_message is not None:
            raise CommandParseError(number_message) from exc
        raise CommandParseError(f"`{label}` must be a number.") from exc
    if value <= 0:
        raise CommandParseError(f"`{label}` must be greater than 0.")
    return value


def extract_single_clause(
    tokens: tuple[str, ...],
    starters: Iterable[str],
    width: int,
    *,
    name: str,
    incomplete_message: Optional[str] = None,
) -> tuple[Optional[ParsedClause], tuple[str, ...]]:
    if width <= 0:
        raise ValueError("clause width must be positive")

    starter_set = normalized_aliases(starters)
    found: Optional[ParsedClause] = None
    remaining: list[str] = []
    index = 0
    while index < len(tokens):
        if normalize_token(tokens[index]) not in starter_set:
            remaining.append(tokens[index])
            index += 1
            continue

        if found is not None:
            raise CommandParseError(f"Only one `{name}` clause is allowed.")
        if index + width > len(tokens):
            raise CommandParseError(incomplete_message or f"`{name}` clause is incomplete.")
        found = ParsedClause(tokens=tokens[index : index + width], index=index)
        index += width

    return found, tuple(remaining)


def require_no_tokens(tokens: tuple[str, ...], *, usage: str) -> None:
    if tokens:
        raise CommandParseError(usage)


def join_free_text(tokens: tuple[str, ...]) -> str:
    return " ".join(token.strip() for token in tokens if token.strip())


def parse_single_value_clause(
    tokens: tuple[str, ...],
    starters: Iterable[str],
    *,
    name: str,
    parser: Callable[[str], T],
    incomplete_message: Optional[str] = None,
) -> tuple[Optional[T], tuple[str, ...]]:
    clause, remaining = extract_single_clause(
        tokens,
        starters,
        2,
        name=name,
        incomplete_message=incomplete_message,
    )
    if clause is None:
        return None, remaining
    return parser(clause.tokens[1]), remaining


def parse_timeout_duration(duration_raw: str, unit_raw: Optional[str] = None) -> tuple[timedelta, str]:
    if unit_raw is None:
        match = re.match(r"^([\-\d\.]+)([a-zA-Z]+)$", duration_raw.strip())
        if not match:
            raise CommandParseError("Invalid duration format. Use a number followed by a unit (e.g., '15m' or '15 min').")
        val_str, unit_str = match.groups()
    else:
        val_str = duration_raw.strip()
        unit_str = unit_raw.strip()

    try:
        val = float(val_str)
    except ValueError as exc:
        raise CommandParseError(f"Invalid duration value: '{val_str}' is not a valid number.") from exc

    if val <= 0:
        raise CommandParseError("Duration must be strictly positive.")

    unit_str = unit_str.lower()
    
    if unit_str in ("m", "min", "mins", "minute", "minutes"):
        delta = timedelta(minutes=val)
        human_unit = "minute" if val == 1 else "minutes"
    elif unit_str in ("h", "hr", "hrs", "hour", "hours"):
        delta = timedelta(hours=val)
        human_unit = "hour" if val == 1 else "hours"
    elif unit_str in ("d", "day", "days"):
        delta = timedelta(days=val)
        human_unit = "day" if val == 1 else "days"
    else:
        raise CommandParseError(f"Invalid duration unit: '{unit_str}'. Supported units are minutes, hours, and days.")

    if delta > timedelta(days=28):
        raise CommandParseError("Timeout duration cannot exceed 28 days.")
    
    val_fmt = f"{int(val)}" if val.is_integer() else f"{val}"
    human_readable = f"{val_fmt} {human_unit}"

    return delta, human_readable
