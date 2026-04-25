from __future__ import annotations

from cogs.help import HelpCog
from cogs.verification import VerificationCog
from services.discord_output import (
    DISCORD_EMBED_DESCRIPTION_LIMIT,
    DISCORD_EMBED_MAX_FIELDS,
    DISCORD_EMBED_TOTAL_CHAR_LIMIT,
    DISCORD_MESSAGE_CHAR_LIMIT,
    clip_text,
    split_embed_field_lines,
    split_text_chunks,
)


def _embed_text_size(embed: object) -> int:
    title = getattr(embed, "title", "") or ""
    description = getattr(embed, "description", "") or ""
    footer = getattr(getattr(embed, "footer", None), "text", "") or ""
    fields = getattr(embed, "fields", [])
    field_size = sum(len(field.name) + len(field.value) for field in fields)
    return len(title) + len(description) + len(footer) + field_size


def test_split_text_chunks_hard_splits_long_line() -> None:
    long_line = "x" * (DISCORD_MESSAGE_CHAR_LIMIT + 57)
    chunks = split_text_chunks(long_line)

    assert len(chunks) == 2
    assert "".join(chunks) == long_line
    assert all(len(chunk) <= DISCORD_MESSAGE_CHAR_LIMIT for chunk in chunks)


def test_split_embed_field_lines_respects_boundary_for_long_single_line() -> None:
    chunks = split_embed_field_lines(["a" * 1300])
    assert chunks == ["a" * 1024, "a" * 276]


def test_clip_text_uses_suffix_with_exact_limit() -> None:
    clipped = clip_text("abcdefghij", limit=7)
    assert clipped == "abcd..."
    assert len(clipped) == 7


def test_roundchanges_embeds_paginate_description_limit() -> None:
    displayed_lines = [
        f"<@{idx}> (`handle{idx}`): **+123** (1500 -> 1623, rank {idx})"
        for idx in range(350)
    ]
    embeds = VerificationCog._build_roundchanges_embeds(
        displayed_lines=displayed_lines,
        contest_name="Very Long Contest Name " * 30,
        contest_id=9999,
        verified_users=350,
        hidden_count=0,
    )

    assert len(embeds) > 1
    assert embeds[0].title.startswith("Server Round Changes")
    for embed in embeds:
        assert len(embed.description or "") <= DISCORD_EMBED_DESCRIPTION_LIMIT
        round_field = next(field for field in embed.fields if field.name == "Round")
        assert len(round_field.value) <= 1024


def test_help_general_help_embed_pages_respect_field_and_total_limits() -> None:
    fields = [(f"Cog {index}", "v" * 1000, False) for index in range(30)]
    embeds = HelpCog._build_general_help_embeds(
        base_description="Use !help <command>",
        fields=fields,
    )

    assert len(embeds) > 1
    for embed in embeds:
        assert len(embed.fields) <= DISCORD_EMBED_MAX_FIELDS
        assert _embed_text_size(embed) <= DISCORD_EMBED_TOTAL_CHAR_LIMIT
