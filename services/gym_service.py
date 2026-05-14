from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime

from db.repository import GymFeatureRepository, VerifiedUser
from services.cf_client import CFRequestError, CFSubmission, CodeforcesClientBase

PARTICIPATION_CACHE_SECONDS = 3600
FORCE_REFRESH_SECONDS = 600
SUBMISSION_CACHE_SECONDS = 3600
GYM_SUBMISSION_CACHE_MAX_ENTRIES = 512


@dataclass(frozen=True, slots=True)
class GymParticipationResult:
    solved_by_discord: dict[int, int]
    unverified_ids: set[int]
    failed_ids: set[int]


def normalize_problem_index(raw: str) -> str:
    return raw.strip().upper()


def normalize_tag(raw: str) -> str:
    return " ".join(raw.strip().lower().split())


def problem_ref(contest_id: int, problem_index: str) -> str:
    return f"{contest_id}/{normalize_problem_index(problem_index)}"


def weight_for_rating(rating: int) -> float:
    if rating < 1200:
        return 1.0
    if rating < 1600:
        return 1.3
    if rating < 1900:
        return 1.7
    if rating < 2100:
        return 2.1
    return 2.6


class GymService:
    def __init__(self, repo: GymFeatureRepository, cf: CodeforcesClientBase) -> None:
        self._repo = repo
        self._cf = cf
        self._submission_cache: OrderedDict[str, tuple[float, list[CFSubmission]]] = OrderedDict()
        self._submission_cache_max_entries = GYM_SUBMISSION_CACHE_MAX_ENTRIES
        self._submission_sem = asyncio.Semaphore(6)

    async def verified_map(self, guild_id: int) -> dict[int, VerifiedUser]:
        rows = await self._repo.get_all(guild_id)
        return {row.discord_id: row for row in rows}

    async def get_submissions(self, handle: str, *, refresh_after_seconds: int = SUBMISSION_CACHE_SECONDS) -> list[CFSubmission]:
        cache_key = handle.lower()
        cached = self._submission_cache.get(cache_key)
        now_ts = time.time()
        if cached is not None:
            ts, payload = cached
            if now_ts - ts < refresh_after_seconds:
                self._submission_cache.move_to_end(cache_key)
                return payload
            self._submission_cache.pop(cache_key, None)
        async with self._submission_sem:
            payload = await self._cf.get_recent_submissions(handle, count=5000)
        self._submission_cache[cache_key] = (time.time(), payload)
        self._submission_cache.move_to_end(cache_key)
        while len(self._submission_cache) > self._submission_cache_max_entries:
            self._submission_cache.popitem(last=False)
        return payload

    async def solved_count_for_contest(
        self,
        handle: str,
        contest_id: int,
        *,
        refresh_after_seconds: int,
    ) -> int:
        submissions = await self.get_submissions(handle, refresh_after_seconds=refresh_after_seconds)
        solved = {
            submission.problem_index
            for submission in submissions
            if submission.verdict == "OK"
            and submission.contest_id == contest_id
            and submission.problem_index is not None
        }
        return len(solved)

    async def user_solved_problem(
        self,
        handle: str,
        contest_id: int,
        problem_index: str,
    ) -> bool:
        submissions = await self.get_submissions(handle, refresh_after_seconds=SUBMISSION_CACHE_SECONDS)
        target_index = normalize_problem_index(problem_index)
        for submission in submissions:
            if submission.verdict != "OK":
                continue
            if submission.contest_id != contest_id:
                continue
            if submission.problem_index != target_index:
                continue
            return True
        return False

    async def can_modify_tags(
        self,
        *,
        member_id: int,
        guild_id: int,
        contest_id: int,
        problem_index: str,
    ) -> tuple[bool, str]:
        verified = await self._repo.get_by_discord_id(member_id, guild_id)
        if verified is None:
            return False, "You must be verified to modify gym tags."
        if verified.rating >= 1600:
            return True, ""
        solved = await self.user_solved_problem(verified.cf_handle, contest_id, problem_index)
        if solved:
            return True, ""
        return False, "Only solvers of this problem or Expert+ users can modify tags."

    async def contest_participation(
        self,
        *,
        guild_id: int,
        contest_id: int,
        training_member_ids: list[int],
        verified_by_id: dict[int, VerifiedUser],
        force: bool,
    ) -> GymParticipationResult:
        now = datetime.now(tz=UTC)
        refresh_age_seconds = FORCE_REFRESH_SECONDS if force else PARTICIPATION_CACHE_SECONDS
        submission_refresh_seconds = FORCE_REFRESH_SECONDS if force else SUBMISSION_CACHE_SECONDS

        cache_rows = await self._repo.get_gym_participation_cache(guild_id, contest_id)
        cache_by_discord = {row.discord_id: row for row in cache_rows}

        solved_by_discord: dict[int, int] = {}
        unverified_ids: set[int] = set()
        failed_ids: set[int] = set()
        pending: list[tuple[int, str, int, bool]] = []

        for member_id in training_member_ids:
            verified = verified_by_id.get(member_id)
            if verified is None:
                unverified_ids.add(member_id)
                continue
            cached = cache_by_discord.get(member_id)
            if cached is not None:
                age = (now - cached.checked_at).total_seconds()
                if age < refresh_age_seconds:
                    solved_by_discord[member_id] = cached.solved_count
                    continue
            previous_solved_count = cached.solved_count if cached is not None else 0
            pending.append((member_id, verified.cf_handle, previous_solved_count, cached is not None))

        async def _refresh_one(
            discord_id: int,
            handle: str,
            previous_solved_count: int,
            has_cached_count: bool,
        ) -> tuple[int, int | None, bool]:
            try:
                fetched_solved_count = await self.solved_count_for_contest(
                    handle,
                    contest_id,
                    refresh_after_seconds=submission_refresh_seconds,
                )
            except CFRequestError:
                return discord_id, previous_solved_count if has_cached_count else None, True
            solved_count = max(previous_solved_count, fetched_solved_count)
            return discord_id, solved_count, False

        if pending:
            refreshed = await asyncio.gather(
                *[
                    _refresh_one(discord_id, handle, previous_solved_count, has_cached_count)
                    for discord_id, handle, previous_solved_count, has_cached_count in pending
                ]
            )
            cache_rows: list[tuple[int, int, int, int, datetime]] = []
            for discord_id, solved_count, failed in refreshed:
                if failed:
                    failed_ids.add(discord_id)
                if solved_count is not None:
                    solved_by_discord[discord_id] = solved_count
                if not failed and solved_count is not None:
                    cache_rows.append((guild_id, contest_id, discord_id, solved_count, now))
            await self._repo.upsert_many_gym_participation_cache(cache_rows)

        return GymParticipationResult(
            solved_by_discord=solved_by_discord,
            unverified_ids=unverified_ids,
            failed_ids=failed_ids,
        )

    async def problem_rating_summary(self, guild_id: int, contest_id: int, problem_index: str) -> dict[str, float]:
        votes = await self._repo.list_gym_problem_rating_votes(guild_id, contest_id, problem_index)
        verified = await self.verified_map(guild_id)

        included = []
        for vote in votes:
            verifier = verified.get(vote.discord_id)
            if verifier is None:
                continue
            included.append((vote.estimated_rating, weight_for_rating(verifier.rating)))

        if not included:
            return {"count": 0.0, "avg": 0.0, "weighted_avg": 0.0}

        avg = sum(v for v, _ in included) / len(included)
        weight_sum = sum(w for _, w in included)
        weighted_avg = sum(v * w for v, w in included) / weight_sum if weight_sum > 0 else avg
        return {
            "count": float(len(included)),
            "avg": avg,
            "weighted_avg": weighted_avg,
        }

    async def gym_quality_summary(self, guild_id: int, contest_id: int) -> dict[str, float]:
        votes = await self._repo.list_gym_quality_votes(guild_id, contest_id)
        verified = await self.verified_map(guild_id)

        included = []
        for vote in votes:
            verifier = verified.get(vote.discord_id)
            if verifier is None:
                continue
            included.append((vote.quality, weight_for_rating(verifier.rating)))

        if not included:
            return {"count": 0.0, "avg": 0.0, "weighted_avg": 0.0}

        avg = sum(v for v, _ in included) / len(included)
        weight_sum = sum(w for _, w in included)
        weighted_avg = sum(v * w for v, w in included) / weight_sum if weight_sum > 0 else avg
        return {
            "count": float(len(included)),
            "avg": avg,
            "weighted_avg": weighted_avg,
        }
