from __future__ import annotations

from dataclasses import dataclass
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
