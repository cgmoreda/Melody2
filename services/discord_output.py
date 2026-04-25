from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import discord

DISCORD_MESSAGE_CHAR_LIMIT = 2000
DISCORD_EMBED_DESCRIPTION_LIMIT = 4096
DISCORD_EMBED_FIELD_VALUE_LIMIT = 1024
DISCORD_EMBED_FIELD_NAME_LIMIT = 256
DISCORD_EMBED_MAX_FIELDS = 25
DISCORD_EMBED_TOTAL_CHAR_LIMIT = 6000


def _hard_split(text: str, limit: int) -> list[str]:
    if limit <= 0:
        raise ValueError("limit must be greater than 0")
    if not text:
        return [""]
    return [text[index:index + limit] for index in range(0, len(text), limit)]


def split_lines_chunks(lines: Sequence[str], *, limit: int = DISCORD_MESSAGE_CHAR_LIMIT) -> list[str]:
    if limit <= 0:
        raise ValueError("limit must be greater than 0")

    chunks: list[str] = []
    current_lines: list[str] = []
    current_size = 0

    for line in lines:
        for segment in _hard_split(line, limit):
            if not current_lines:
                current_lines = [segment]
                current_size = len(segment)
                continue

            addition = 1 + len(segment)  # newline + segment
            if current_size + addition <= limit:
                current_lines.append(segment)
                current_size += addition
                continue

            chunks.append("\n".join(current_lines))
            current_lines = [segment]
            current_size = len(segment)

    if current_lines:
        chunks.append("\n".join(current_lines))

    if not chunks:
        return [""]
    return chunks


def split_text_chunks(text: str, *, limit: int = DISCORD_MESSAGE_CHAR_LIMIT) -> list[str]:
    return split_lines_chunks(text.split("\n"), limit=limit)


def clip_text(text: str, *, limit: int, suffix: str = "...") -> str:
    if limit <= 0:
        raise ValueError("limit must be greater than 0")
    if len(text) <= limit:
        return text
    if len(suffix) >= limit:
        return suffix[:limit]
    return f"{text[: limit - len(suffix)]}{suffix}"


def clip_embed_description(text: str) -> str:
    return clip_text(text, limit=DISCORD_EMBED_DESCRIPTION_LIMIT)


def clip_embed_field_name(text: str) -> str:
    return clip_text(text, limit=DISCORD_EMBED_FIELD_NAME_LIMIT)


def split_embed_description_chunks(text: str) -> list[str]:
    return split_text_chunks(text, limit=DISCORD_EMBED_DESCRIPTION_LIMIT)


def split_embed_field_value_chunks(text: str) -> list[str]:
    return split_text_chunks(text, limit=DISCORD_EMBED_FIELD_VALUE_LIMIT)


def split_embed_field_lines(lines: Sequence[str]) -> list[str]:
    return split_lines_chunks(lines, limit=DISCORD_EMBED_FIELD_VALUE_LIMIT)


async def send_context_text_chunks(
    ctx: Any,
    text: str,
    *,
    limit: int = DISCORD_MESSAGE_CHAR_LIMIT,
) -> None:
    for chunk in split_text_chunks(text, limit=limit):
        if not chunk:
            continue
        await ctx.send(chunk)


async def send_context_lines_chunks(
    ctx: Any,
    lines: Sequence[str],
    *,
    limit: int = DISCORD_MESSAGE_CHAR_LIMIT,
) -> None:
    for chunk in split_lines_chunks(lines, limit=limit):
        if not chunk:
            continue
        await ctx.send(chunk)


async def send_interaction_text_chunks(
    interaction: discord.Interaction,
    text: str,
    *,
    ephemeral: bool = False,
    limit: int = DISCORD_MESSAGE_CHAR_LIMIT,
) -> None:
    chunks = split_text_chunks(text, limit=limit)
    sent_any = False
    initial_response_done = interaction.response.is_done()

    for chunk in chunks:
        if not chunk:
            continue
        if not initial_response_done:
            await interaction.response.send_message(chunk, ephemeral=ephemeral)
            initial_response_done = True
        else:
            await interaction.followup.send(chunk, ephemeral=ephemeral)
        sent_any = True

    if not sent_any and not initial_response_done:
        await interaction.response.send_message("\u200b", ephemeral=ephemeral)


async def send_interaction_lines_chunks(
    interaction: discord.Interaction,
    lines: Sequence[str],
    *,
    ephemeral: bool = False,
    limit: int = DISCORD_MESSAGE_CHAR_LIMIT,
) -> None:
    await send_interaction_text_chunks(
        interaction,
        "\n".join(lines),
        ephemeral=ephemeral,
        limit=limit,
    )
