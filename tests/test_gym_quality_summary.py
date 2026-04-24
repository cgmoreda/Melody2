from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cogs.gym import GymCog
from db.repository import GymQualityVote, VerifiedUser


class _FakeRepo:
    def __init__(self, verified: list[VerifiedUser], votes: list[GymQualityVote]) -> None:
        self._verified = verified
        self._votes = votes

    async def get_all(self, guild_id: int) -> list[VerifiedUser]:
        return [row for row in self._verified if row.guild_id == guild_id]

    async def list_gym_quality_votes(self, guild_id: int, contest_id: int) -> list[GymQualityVote]:
        return [row for row in self._votes if row.guild_id == guild_id and row.contest_id == contest_id]


@pytest.mark.asyncio
async def test_gym_quality_summary_weighted_and_verified_only() -> None:
    guild_id = 1
    contest_id = 2062
    now = datetime.now(tz=UTC)

    verified = [
        VerifiedUser(discord_id=11, cf_handle="low", rating=1000, guild_id=guild_id),
        VerifiedUser(discord_id=22, cf_handle="high", rating=2300, guild_id=guild_id),
    ]
    votes = [
        GymQualityVote(guild_id, contest_id, 11, 1, now, now),
        GymQualityVote(guild_id, contest_id, 22, 5, now, now),
        GymQualityVote(guild_id, contest_id, 33, 5, now, now),  # ignored (not verified)
    ]

    cog = GymCog(
        bot=object(),  # type: ignore[arg-type]
        repo=_FakeRepo(verified, votes),  # type: ignore[arg-type]
        cf=object(),  # type: ignore[arg-type]
        config_service=object(),  # type: ignore[arg-type]
    )
    summary = await cog._gym_quality_summary(guild_id, contest_id)

    assert summary["count"] == pytest.approx(2.0)
    assert summary["avg"] == pytest.approx(3.0)
    assert summary["weighted_avg"] == pytest.approx((1.0 + 13.0) / 3.6, rel=1e-3)
    assert summary["weighted_avg"] > summary["avg"]


@pytest.mark.asyncio
async def test_gym_quality_summary_empty_when_no_verified_votes() -> None:
    guild_id = 1
    contest_id = 2062
    now = datetime.now(tz=UTC)

    votes = [GymQualityVote(guild_id, contest_id, 44, 4, now, now)]
    cog = GymCog(
        bot=object(),  # type: ignore[arg-type]
        repo=_FakeRepo([], votes),  # type: ignore[arg-type]
        cf=object(),  # type: ignore[arg-type]
        config_service=object(),  # type: ignore[arg-type]
    )
    summary = await cog._gym_quality_summary(guild_id, contest_id)

    assert summary == {"count": 0.0, "avg": 0.0, "weighted_avg": 0.0}
