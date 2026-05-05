from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

import cogs.gym as gym_module
from cogs.gym import GymCog
from db.repository import GymContest, VerifiedUser


class _FakeRepo:
    def __init__(self, gyms: list[GymContest]) -> None:
        self._gyms = gyms

    async def list_gym_contests(self, guild_id: int) -> list[GymContest]:
        return list(self._gyms)


class _FakeConfig:
    async def get_text(self, guild_id: int, key: str) -> str:
        return "training"


class _FakeMember:
    def __init__(self, member_id: int, display_name: str) -> None:
        self.id = member_id
        self.display_name = display_name
        self.mention = f"<@{member_id}>"
        self.bot = False


class _FakeGuild:
    id = 1
    roles: list[Any] = []


class _FakeContext:
    def __init__(self) -> None:
        self.guild = _FakeGuild()
        self.sent: list[str] = []

    async def send(self, message: str, **kwargs: object) -> None:
        self.sent.append(message)


def test_parse_gald_args_rejects_duplicate_contest_ids() -> None:
    with pytest.raises(ValueError, match="Only one `contest_id`"):
        GymCog._parse_gald_args(("2062", "2063"))


@pytest.mark.asyncio
async def test_gald_accepts_force_teams_contest_in_any_order(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    repo = _FakeRepo([GymContest(1, 2062, "team", 10, now)])
    cog = GymCog(
        bot=object(),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        cf=object(),  # type: ignore[arg-type]
        config_service=_FakeConfig(),  # type: ignore[arg-type]
    )
    member = _FakeMember(100, "Trainee")
    calls: list[dict[str, object]] = []
    sent_chunks: list[list[str]] = []

    async def _training_members(guild: object) -> list[_FakeMember]:
        return [member]

    async def _verified_map(guild_id: int) -> dict[int, VerifiedUser]:
        return {100: VerifiedUser(discord_id=100, cf_handle="tourist", rating=3000, guild_id=1)}

    async def _contest_participation(
        guild_id: int,
        contest_id: int,
        training_members: list[_FakeMember],
        verified_by_id: dict[int, VerifiedUser],
        *,
        force: bool,
    ) -> tuple[dict[int, int], set[int]]:
        calls.append({"contest_id": contest_id, "force": force})
        return {100: 0}, set()

    async def _fake_send_chunks(ctx: object, lines: list[str]) -> None:
        sent_chunks.append(lines)

    monkeypatch.setattr(cog, "_training_members", _training_members)
    monkeypatch.setattr(cog, "_verified_map", _verified_map)
    monkeypatch.setattr(cog, "_contest_participation", _contest_participation)
    monkeypatch.setattr(gym_module, "send_context_lines_chunks", _fake_send_chunks)

    ctx = _FakeContext()
    await cog.gald.callback(cog, ctx, "force", "teams", "2062")  # type: ignore[union-attr]

    assert calls == [{"contest_id": 2062, "force": True}]
    assert len(sent_chunks) == 1
    output = "\n".join(sent_chunks[0])
    assert "Contest `2062` (team)" in output
    assert "cache mode: **force(10m)**" in output
    assert "<@100>" in output
