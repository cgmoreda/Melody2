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
        """Return user info or ``None`` if the handle does not exist."""

    @abc.abstractmethod
    async def get_rating_history(self, handle: str) -> list[CFContestChange]:
        """Return rating change history for a Codeforces user."""

    @abc.abstractmethod
    async def get_recent_submissions(self, handle: str, count: int = 500) -> list[CFSubmission]:
        """Return recent submissions for activity stats."""


class CodeforcesClient(CodeforcesClientBase):
    """Concrete Codeforces API client backed by ``aiohttp``."""

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
            ) as resp:
                if resp.status != 200:
                    logger.warning("CF API returned status %s for endpoint %s", resp.status, endpoint)
                    return None
                data = await resp.json()
        except (aiohttp.ClientError, TimeoutError) as exc:
            logger.error("CF API request failed: %s", exc)
            return None

        if data.get("status") != "OK":
            return None
        return data

    async def get_user(self, handle: str) -> Optional[CFUserInfo]:
        data = await self._get("user.info", {"handles": handle})
        if data is None or not data.get("result"):
            return None

        user = data["result"][0]
        return CFUserInfo(
            handle=user.get("handle", handle),
            first_name=user.get("firstName"),
            rating=user.get("rating", 0),
            max_rating=user.get("maxRating", user.get("rating", 0)),
            rank=user.get("rank"),
            max_rank=user.get("maxRank"),
            country=user.get("country"),
            city=user.get("city"),
            organization=user.get("organization"),
            contribution=user.get("contribution", 0),
            friend_of_count=user.get("friendOfCount", 0),
            avatar_url=user.get("avatar"),
            title_photo_url=user.get("titlePhoto"),
        )

    async def get_rating_history(self, handle: str) -> list[CFContestChange]:
        data = await self._get("user.rating", {"handle": handle})
        if data is None:
            return []
        result = data.get("result", [])
        history: list[CFContestChange] = []
        for row in result:
            history.append(
                CFContestChange(
                    contest_id=row.get("contestId", 0),
                    contest_name=row.get("contestName", "Unknown"),
                    rank=row.get("rank", 0),
                    old_rating=row.get("oldRating", 0),
                    new_rating=row.get("newRating", 0),
                )
            )
        return history

    async def get_recent_submissions(self, handle: str, count: int = 500) -> list[CFSubmission]:
        safe_count = max(1, min(count, 1000))
        data = await self._get("user.status", {"handle": handle, "from": 1, "count": safe_count})
        if data is None:
            return []
        result = data.get("result", [])
        submissions: list[CFSubmission] = []
        for row in result:
            problem = row.get("problem", {}) or {}
            tags = tuple(tag for tag in problem.get("tags", []) if isinstance(tag, str))
            contest_id = problem.get("contestId")
            index = problem.get("index")
            problemset_name = problem.get("problemsetName")
            problem_key: Optional[str] = None
            if contest_id and index:
                problem_key = f"{contest_id}{index}"
            elif problemset_name and index:
                problem_key = f"{problemset_name}:{index}"

            submissions.append(
                CFSubmission(
                    verdict=row.get("verdict"),
                    tags=tags,
                    problem_key=problem_key,
                )
            )
        return submissions
