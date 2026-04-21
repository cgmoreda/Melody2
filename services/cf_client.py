from __future__ import annotations

import abc
import logging
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

CF_API_BASE = "https://codeforces.com/api"


@dataclass(frozen=True, slots=True)
class CFUserInfo:
    """Subset of Codeforces user.info relevant to verification."""

    handle: str
    first_name: Optional[str]
    rating: int
    max_rating: int
    rank: Optional[str]
    max_rank: Optional[str]
    country: Optional[str]
    city: Optional[str]
    organization: Optional[str]
    contribution: int
    friend_of_count: int
    avatar_url: Optional[str]
    title_photo_url: Optional[str]


@dataclass(frozen=True, slots=True)
class CFContestChange:
    contest_id: int
    contest_name: str
    rank: int
    old_rating: int
    new_rating: int


@dataclass(frozen=True, slots=True)
class CFSubmission:
    verdict: Optional[str]
    tags: tuple[str, ...]
    problem_key: Optional[str]


class CodeforcesClientBase(abc.ABC):
    """Abstraction for Codeforces API access."""

    @abc.abstractmethod
    async def get_user(self, handle: str) -> Optional[CFUserInfo]:
        """Return user info or None if the handle does not exist."""

    @abc.abstractmethod
    async def get_rating_history(self, handle: str) -> list[CFContestChange]:
        """Return rating change history for a Codeforces user."""

    @abc.abstractmethod
    async def get_recent_submissions(self, handle: str, count: int = 500) -> list[CFSubmission]:
        """Return recent submissions for activity stats."""


class CodeforcesClient(CodeforcesClientBase):
    """Concrete Codeforces API client backed by aiohttp."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def _get(self, endpoint: str, params: dict[str, object]) -> Optional[dict]:
        url = f"{CF_API_BASE}/{endpoint}"
        full_params = {**params, "_": int(time.time())}

        try:
            async with self._session.get(
                url,
                params=full_params,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                if response.status != 200:
                    logger.warning("CF API returned status %s for endpoint %s", response.status, endpoint)
                    return None
                payload = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            logger.warning("CF API request failed for endpoint %s: %s", endpoint, exc)
            return None

        if not isinstance(payload, dict):
            return None
        if payload.get("status") != "OK":
            return None
        return payload

    async def get_user(self, handle: str) -> Optional[CFUserInfo]:
        data = await self._get("user.info", {"handles": handle})
        if data is None:
            return None

        result = data.get("result")
        if not isinstance(result, list) or not result:
            return None

        user = result[0]
        if not isinstance(user, dict):
            return None

        current_rating = int(user.get("rating", 0) or 0)
        max_rating = int(user.get("maxRating", current_rating) or current_rating)

        return CFUserInfo(
            handle=str(user.get("handle", handle)),
            first_name=user.get("firstName") if isinstance(user.get("firstName"), str) else None,
            rating=current_rating,
            max_rating=max_rating,
            rank=user.get("rank") if isinstance(user.get("rank"), str) else None,
            max_rank=user.get("maxRank") if isinstance(user.get("maxRank"), str) else None,
            country=user.get("country") if isinstance(user.get("country"), str) else None,
            city=user.get("city") if isinstance(user.get("city"), str) else None,
            organization=user.get("organization") if isinstance(user.get("organization"), str) else None,
            contribution=int(user.get("contribution", 0) or 0),
            friend_of_count=int(user.get("friendOfCount", 0) or 0),
            avatar_url=user.get("avatar") if isinstance(user.get("avatar"), str) else None,
            title_photo_url=user.get("titlePhoto") if isinstance(user.get("titlePhoto"), str) else None,
        )

    async def get_rating_history(self, handle: str) -> list[CFContestChange]:
        data = await self._get("user.rating", {"handle": handle})
        if data is None:
            return []

        result = data.get("result")
        if not isinstance(result, list):
            return []

        history: list[CFContestChange] = []
        for row in result:
            if not isinstance(row, dict):
                continue
            history.append(
                CFContestChange(
                    contest_id=int(row.get("contestId", 0) or 0),
                    contest_name=str(row.get("contestName", "Unknown")),
                    rank=int(row.get("rank", 0) or 0),
                    old_rating=int(row.get("oldRating", 0) or 0),
                    new_rating=int(row.get("newRating", 0) or 0),
                )
            )
        return history

    async def get_recent_submissions(self, handle: str, count: int = 500) -> list[CFSubmission]:
        safe_count = max(1, min(count, 1000))
        data = await self._get("user.status", {"handle": handle, "from": 1, "count": safe_count})
        if data is None:
            return []

        result = data.get("result")
        if not isinstance(result, list):
            return []

        submissions: list[CFSubmission] = []
        for row in result:
            if not isinstance(row, dict):
                continue

            problem = row.get("problem")
            if not isinstance(problem, dict):
                problem = {}

            raw_tags = problem.get("tags")
            tags = tuple(tag for tag in raw_tags if isinstance(tag, str)) if isinstance(raw_tags, list) else ()

            contest_id = problem.get("contestId")
            index = problem.get("index")
            problemset_name = problem.get("problemsetName")

            problem_key: Optional[str] = None
            if isinstance(contest_id, int) and isinstance(index, str):
                problem_key = f"{contest_id}{index}"
            elif isinstance(problemset_name, str) and isinstance(index, str):
                problem_key = f"{problemset_name}:{index}"

            verdict = row.get("verdict")
            submissions.append(
                CFSubmission(
                    verdict=verdict if isinstance(verdict, str) else None,
                    tags=tags,
                    problem_key=problem_key,
                )
            )

        return submissions