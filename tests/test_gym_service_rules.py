from __future__ import annotations

from datetime import UTC, datetime

import pytest

from db.repository import GymProblemRatingVote, VerifiedUser
from services.cf_client import CFSubmission
from services.gym_service import GymService


class _FakeRepo:
    def __init__(
        self,
        *,
        verified_by_member: dict[tuple[int, int], VerifiedUser] | None = None,
        all_verified: list[VerifiedUser] | None = None,
        problem_votes: list[GymProblemRatingVote] | None = None,
    ) -> None:
        self._verified_by_member = verified_by_member or {}
        self._all_verified = all_verified or []
        self._problem_votes = problem_votes or []

    async def get_by_discord_id(self, discord_id: int, guild_id: int) -> VerifiedUser | None:
        return self._verified_by_member.get((guild_id, discord_id))

    async def get_all(self, guild_id: int) -> list[VerifiedUser]:
        rows = [row for row in self._all_verified if row.guild_id == guild_id]
        for (row_guild_id, _), row in self._verified_by_member.items():
            if row_guild_id == guild_id:
                rows.append(row)
        return rows

    async def list_gym_problem_rating_votes(
        self,
        guild_id: int,
        contest_id: int,
        problem_index: str,
    ) -> list[GymProblemRatingVote]:
        return [
            row
            for row in self._problem_votes
            if row.guild_id == guild_id
            and row.contest_id == contest_id
            and row.problem_index == problem_index
        ]


class _FakeCF:
    def __init__(self, submissions: list[CFSubmission]) -> None:
        self._submissions = submissions
        self.calls = 0

    async def get_recent_submissions(self, handle: str, count: int = 5000) -> list[CFSubmission]:
        self.calls += 1
        return list(self._submissions)


@pytest.mark.asyncio
async def test_can_modify_tags_requires_verification() -> None:
    service = GymService(repo=_FakeRepo(), cf=_FakeCF([]))  # type: ignore[arg-type]

    allowed, reason = await service.can_modify_tags(
        member_id=11,
        guild_id=22,
        contest_id=33,
        problem_index="A",
    )

    assert allowed is False
    assert "must be verified" in reason.lower()


@pytest.mark.asyncio
async def test_can_modify_tags_allows_expert_without_submission_lookup() -> None:
    verified = VerifiedUser(discord_id=55, cf_handle="expert_user", rating=1700, guild_id=44)
    fake_cf = _FakeCF([])
    service = GymService(
        repo=_FakeRepo(verified_by_member={(44, 55): verified}),
        cf=fake_cf,  # type: ignore[arg-type]
    )

    allowed, reason = await service.can_modify_tags(
        member_id=55,
        guild_id=44,
        contest_id=1000,
        problem_index="A",
    )

    assert allowed is True
    assert reason == ""
    assert fake_cf.calls == 0


@pytest.mark.asyncio
async def test_can_modify_tags_allows_solver_when_rating_below_expert() -> None:
    verified = VerifiedUser(discord_id=66, cf_handle="solver_user", rating=1400, guild_id=77)
    fake_cf = _FakeCF(
        [
            CFSubmission(
                verdict="OK",
                tags=("dp",),
                problem_key="2062A",
                contest_id=2062,
                problem_index="A",
            )
        ]
    )
    service = GymService(
        repo=_FakeRepo(verified_by_member={(77, 66): verified}),
        cf=fake_cf,  # type: ignore[arg-type]
    )

    allowed, reason = await service.can_modify_tags(
        member_id=66,
        guild_id=77,
        contest_id=2062,
        problem_index="a",
    )

    assert allowed is True
    assert reason == ""
    assert fake_cf.calls == 1


@pytest.mark.asyncio
async def test_problem_rating_summary_ignores_unverified_votes_and_applies_weights() -> None:
    now = datetime.now(tz=UTC)
    guild_id = 500
    contest_id = 999
    problem_index = "B"

    verified = [
        VerifiedUser(discord_id=1, cf_handle="low", rating=1200, guild_id=guild_id),
        VerifiedUser(discord_id=2, cf_handle="high", rating=2200, guild_id=guild_id),
    ]
    votes = [
        GymProblemRatingVote(guild_id, contest_id, problem_index, 1, 1000, now, now),
        GymProblemRatingVote(guild_id, contest_id, problem_index, 2, 2000, now, now),
        GymProblemRatingVote(guild_id, contest_id, problem_index, 3, 3000, now, now),  # ignored
    ]

    service = GymService(
        repo=_FakeRepo(all_verified=verified, problem_votes=votes),  # type: ignore[arg-type]
        cf=_FakeCF([]),  # type: ignore[arg-type]
    )
    summary = await service.problem_rating_summary(guild_id, contest_id, problem_index)

    assert summary["count"] == pytest.approx(2.0)
    assert summary["avg"] == pytest.approx(1500.0)
    assert summary["weighted_avg"] == pytest.approx((1000 * 1.3 + 2000 * 2.6) / (1.3 + 2.6))
