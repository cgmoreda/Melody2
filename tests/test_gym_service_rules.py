from __future__ import annotations

from datetime import UTC, datetime

import pytest

from db.repository import GymParticipationCache, GymProblemRatingVote, VerifiedUser
from services.cf_client import CFRequestError, CFSubmission
from services.gym_service import GymService


class _FakeRepo:
    def __init__(
        self,
        *,
        verified_by_member: dict[tuple[int, int], VerifiedUser] | None = None,
        all_verified: list[VerifiedUser] | None = None,
        problem_votes: list[GymProblemRatingVote] | None = None,
        participation_cache: list[GymParticipationCache] | None = None,
    ) -> None:
        self._verified_by_member = verified_by_member or {}
        self._all_verified = all_verified or []
        self._problem_votes = problem_votes or []
        self._participation_cache = participation_cache or []
        self.participation_upserts: list[tuple[int, int, int, int, datetime]] = []

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

    async def get_gym_participation_cache(self, guild_id: int, contest_id: int) -> list[GymParticipationCache]:
        return [
            row
            for row in self._participation_cache
            if row.guild_id == guild_id and row.contest_id == contest_id
        ]

    async def upsert_gym_participation_cache(
        self,
        guild_id: int,
        contest_id: int,
        discord_id: int,
        solved_count: int,
        checked_at: datetime,
    ) -> None:
        self.participation_upserts.append((guild_id, contest_id, discord_id, solved_count, checked_at))

    async def upsert_many_gym_participation_cache(
        self,
        rows: list[tuple[int, int, int, int, datetime]],
    ) -> None:
        self.participation_upserts.extend(rows)


class _FakeCF:
    def __init__(self, submissions: list[CFSubmission]) -> None:
        self._submissions = submissions
        self.calls = 0

    async def get_recent_submissions(self, handle: str, count: int = 5000) -> list[CFSubmission]:
        self.calls += 1
        return list(self._submissions)


class _FailingCF:
    def __init__(self) -> None:
        self.calls = 0

    async def get_recent_submissions(self, handle: str, count: int = 5000) -> list[CFSubmission]:
        self.calls += 1
        raise CFRequestError(
            endpoint="user.status",
            failure_kind="network",
            requested_url=f"https://codeforces.com/api/user.status?handle={handle}",
        )


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
async def test_contest_participation_marks_failed_refresh_without_false_zero() -> None:
    guild_id = 77
    contest_id = 2062
    verified = VerifiedUser(discord_id=66, cf_handle="solver_user", rating=1400, guild_id=guild_id)
    service = GymService(
        repo=_FakeRepo(verified_by_member={(guild_id, 66): verified}),  # type: ignore[arg-type]
        cf=_FailingCF(),  # type: ignore[arg-type]
    )

    result = await service.contest_participation(
        guild_id=guild_id,
        contest_id=contest_id,
        training_member_ids=[66],
        verified_by_id={66: verified},
        force=True,
    )

    assert result.failed_ids == {66}
    assert result.solved_by_discord == {}
    assert result.unverified_ids == set()


@pytest.mark.asyncio
async def test_contest_participation_keeps_stale_cached_count_on_refresh_failure() -> None:
    guild_id = 77
    contest_id = 2062
    checked_at = datetime(2026, 1, 1, tzinfo=UTC)
    verified = VerifiedUser(discord_id=66, cf_handle="solver_user", rating=1400, guild_id=guild_id)
    repo = _FakeRepo(
        verified_by_member={(guild_id, 66): verified},
        participation_cache=[
            GymParticipationCache(guild_id, contest_id, 66, solved_count=2, checked_at=checked_at)
        ],
    )
    service = GymService(repo=repo, cf=_FailingCF())  # type: ignore[arg-type]

    result = await service.contest_participation(
        guild_id=guild_id,
        contest_id=contest_id,
        training_member_ids=[66],
        verified_by_id={66: verified},
        force=True,
    )

    assert result.failed_ids == {66}
    assert result.solved_by_discord == {66: 2}
    assert repo.participation_upserts == []


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


@pytest.mark.asyncio
async def test_gym_submission_cache_evicts_least_recent_entry() -> None:
    service = GymService(repo=_FakeRepo(), cf=_FakeCF([]))  # type: ignore[arg-type]
    service._submission_cache_max_entries = 2

    await service.get_submissions("a")
    await service.get_submissions("b")
    await service.get_submissions("a")
    await service.get_submissions("c")

    assert list(service._submission_cache) == ["a", "c"]
