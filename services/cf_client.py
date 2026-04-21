from __future__ import annotations

import abc
import asyncio
import hashlib
import logging
import os
import random
import string
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import aiohttp

logger = logging.getLogger(__name__)

CF_API_BASE = "https://codeforces.com/api"


def build_api_sig(rand_prefix: str, method: str, params: dict[str, object], api_secret: str) -> str:
    """Build Codeforces apiSig value using SHA-512."""
    serialized = urlencode(sorted((key, str(value)) for key, value in params.items()))
    payload = f"{rand_prefix}/{method}?{serialized}#{api_secret}"
    digest = hashlib.sha512(payload.encode("utf-8")).hexdigest()
    return f"{rand_prefix}{digest}"


@dataclass(frozen=True, slots=True)
class CFUserInfo:
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
    handle: Optional[str] = None


@dataclass(frozen=True, slots=True)
class CFSubmission:
    verdict: Optional[str]
    tags: tuple[str, ...]
    problem_key: Optional[str]


@dataclass(frozen=True, slots=True)
class CFStandingRow:
    rank: int
    points: float
    penalty: int
    participant_type: str
    handles: tuple[str, ...]
    is_official: bool


@dataclass(frozen=True, slots=True)
class CFStandingsPage:
    contest_id: int
    contest_name: str
    phase: str
    rows: list[CFStandingRow]


@dataclass(frozen=True, slots=True)
class CFRequestError:
    endpoint: str
    requested_url: str
    detail: str


class CodeforcesClientBase(abc.ABC):
    @abc.abstractmethod
    async def get_user(self, handle: str) -> Optional[CFUserInfo]:
        ...

    @abc.abstractmethod
    async def get_rating_history(self, handle: str) -> list[CFContestChange]:
        ...

    @abc.abstractmethod
    async def get_recent_submissions(self, handle: str, count: int = 500) -> list[CFSubmission]:
        ...

    @abc.abstractmethod
    async def get_contest_standings_page(
        self,
        contest_id: int,
        start_from: int,
        count: int,
        *,
        show_unofficial: bool,
        handles: Optional[list[str]] = None,
    ) -> Optional[CFStandingsPage]:
        ...

    @abc.abstractmethod
    async def get_contest_rating_changes(self, contest_id: int) -> list[CFContestChange]:
        ...


class CodeforcesClient(CodeforcesClientBase):
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._api_key = os.getenv("CF_API_KEY", "").strip()
        self._api_secret = os.getenv("CF_API_SECRET", "").strip()
        self._cache_ttl_seconds = max(0, int(os.getenv("CACHE_TTL_SECONDS", "60")))
        self._timeout_seconds = max(5, int(os.getenv("REQUEST_TIMEOUT_SECONDS", "20")))
        self._max_retries = max(1, int(os.getenv("CF_MAX_RETRIES", "3")))
        self.standings_page_size = max(10, min(5000, int(os.getenv("CF_STANDINGS_PAGE_SIZE", "500"))))
        self._cache: dict[str, tuple[float, dict]] = {}
        self._last_error: Optional[CFRequestError] = None

    @property
    def last_error(self) -> Optional[CFRequestError]:
        return self._last_error

    def _cache_get(self, cache_key: str) -> Optional[dict]:
        if self._cache_ttl_seconds <= 0:
            return None
        cached = self._cache.get(cache_key)
        if cached is None:
            return None
        expires_at, payload = cached
        if expires_at < time.time():
            self._cache.pop(cache_key, None)
            return None
        return payload

    def _cache_set(self, cache_key: str, payload: dict) -> None:
        if self._cache_ttl_seconds <= 0:
            return
        self._cache[cache_key] = (time.time() + self._cache_ttl_seconds, payload)

    def _signed_params(self, endpoint: str, params: dict[str, object]) -> dict[str, object]:
        if not self._api_key or not self._api_secret:
            raise RuntimeError("CF_API_KEY and CF_API_SECRET must be set for signed requests")

        signed = dict(params)
        signed["apiKey"] = self._api_key
        signed["time"] = int(time.time())
        rand_prefix = "".join(random.choice(string.digits) for _ in range(6))
        signed["apiSig"] = build_api_sig(rand_prefix, endpoint, signed, self._api_secret)
        return signed

    async def _get(self, endpoint: str, params: dict[str, object], *, require_auth: bool = False) -> Optional[dict]:
        self._last_error = None
        request_params = dict(params)
        if require_auth:
            try:
                request_params = self._signed_params(endpoint, request_params)
            except RuntimeError as exc:
                logger.warning("Signed request for %s failed: %s", endpoint, exc)
                self._last_error = CFRequestError(
                    endpoint=endpoint,
                    requested_url=f"{CF_API_BASE}/{endpoint}",
                    detail=str(exc),
                )
                return None

        cache_key = f"{endpoint}?{urlencode(sorted((k, str(v)) for k, v in request_params.items()))}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        url = f"{CF_API_BASE}/{endpoint}"
        last_error_detail = "Unknown error"
        delay = 0.7
        for attempt in range(1, self._max_retries + 1):
            try:
                async with self._session.get(
                    url,
                    params=request_params,
                    timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
                ) as response:
                    if response.status != 200:
                        last_error_detail = f"HTTP {response.status}"
                        if 500 <= response.status < 600 and attempt < self._max_retries:
                            await asyncio.sleep(delay)
                            delay *= 2
                            continue
                        logger.warning("CF API status %s for %s", response.status, endpoint)
                        self._last_error = CFRequestError(
                            endpoint=endpoint,
                            requested_url=str(response.url),
                            detail=last_error_detail,
                        )
                        return None
                    payload = await response.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
                last_error_detail = str(exc)
                if attempt < self._max_retries:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                logger.warning("CF API request failed for %s: %s", endpoint, exc)
                self._last_error = CFRequestError(
                    endpoint=endpoint,
                    requested_url=f"{url}?{urlencode(sorted((k, str(v)) for k, v in request_params.items()))}",
                    detail=last_error_detail,
                )
                return None

            if not isinstance(payload, dict):
                self._last_error = CFRequestError(
                    endpoint=endpoint,
                    requested_url=f"{url}?{urlencode(sorted((k, str(v)) for k, v in request_params.items()))}",
                    detail="Response was not JSON object",
                )
                return None
            if payload.get("status") != "OK":
                comment = payload.get("comment")
                last_error_detail = str(comment) if comment is not None else "Codeforces returned non-OK status"
                if attempt < self._max_retries and isinstance(comment, str) and "limit" in comment.lower():
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                self._last_error = CFRequestError(
                    endpoint=endpoint,
                    requested_url=f"{url}?{urlencode(sorted((k, str(v)) for k, v in request_params.items()))}",
                    detail=last_error_detail,
                )
                return None

            self._cache_set(cache_key, payload)
            return payload

        self._last_error = CFRequestError(
            endpoint=endpoint,
            requested_url=f"{url}?{urlencode(sorted((k, str(v)) for k, v in request_params.items()))}",
            detail=last_error_detail,
        )
        return None

    async def get_user(self, handle: str) -> Optional[CFUserInfo]:
        data = await self._get("user.info", {"handles": handle})
        if data is None:
            return None

        result = data.get("result")
        if not isinstance(result, list) or not result or not isinstance(result[0], dict):
            return None
        user = result[0]

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

    async def get_users(self, handles: list[str]) -> dict[str, CFUserInfo]:
        unique_handles = sorted({handle.strip() for handle in handles if handle.strip()})
        if not unique_handles:
            return {}

        result: dict[str, CFUserInfo] = {}
        chunk_size = 100
        for idx in range(0, len(unique_handles), chunk_size):
            batch = unique_handles[idx : idx + chunk_size]
            data = await self._get("user.info", {"handles": ";".join(batch)})
            if data is None:
                continue
            rows = data.get("result")
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                info = CFUserInfo(
                    handle=str(row.get("handle", "")),
                    first_name=row.get("firstName") if isinstance(row.get("firstName"), str) else None,
                    rating=int(row.get("rating", 0) or 0),
                    max_rating=int(row.get("maxRating", row.get("rating", 0) or 0) or 0),
                    rank=row.get("rank") if isinstance(row.get("rank"), str) else None,
                    max_rank=row.get("maxRank") if isinstance(row.get("maxRank"), str) else None,
                    country=row.get("country") if isinstance(row.get("country"), str) else None,
                    city=row.get("city") if isinstance(row.get("city"), str) else None,
                    organization=row.get("organization") if isinstance(row.get("organization"), str) else None,
                    contribution=int(row.get("contribution", 0) or 0),
                    friend_of_count=int(row.get("friendOfCount", 0) or 0),
                    avatar_url=row.get("avatar") if isinstance(row.get("avatar"), str) else None,
                    title_photo_url=row.get("titlePhoto") if isinstance(row.get("titlePhoto"), str) else None,
                )
                if info.handle:
                    result[info.handle.lower()] = info
        return result

    async def get_rating_history(self, handle: str) -> list[CFContestChange]:
        data = await self._get("user.rating", {"handle": handle})
        if data is None:
            return []
        rows = data.get("result")
        if not isinstance(rows, list):
            return []
        result: list[CFContestChange] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            result.append(
                CFContestChange(
                    contest_id=int(row.get("contestId", 0) or 0),
                    contest_name=str(row.get("contestName", "Unknown")),
                    rank=int(row.get("rank", 0) or 0),
                    old_rating=int(row.get("oldRating", 0) or 0),
                    new_rating=int(row.get("newRating", 0) or 0),
                    handle=handle,
                )
            )
        return result

    async def get_recent_submissions(self, handle: str, count: int = 500) -> list[CFSubmission]:
        safe_count = max(1, min(count, 1000))
        data = await self._get("user.status", {"handle": handle, "from": 1, "count": safe_count})
        if data is None:
            return []
        rows = data.get("result")
        if not isinstance(rows, list):
            return []

        submissions: list[CFSubmission] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            problem = row.get("problem") if isinstance(row.get("problem"), dict) else {}
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

    async def get_contest_standings_page(
        self,
        contest_id: int,
        start_from: int,
        count: int,
        *,
        show_unofficial: bool,
        handles: Optional[list[str]] = None,
    ) -> Optional[CFStandingsPage]:
        params: dict[str, object] = {
            "contestId": contest_id,
            "from": max(1, start_from),
            "count": max(1, count),
            "showUnofficial": str(show_unofficial).lower(),
        }
        if handles:
            params["handles"] = ";".join(handles)

        data = await self._get("contest.standings", params, require_auth=True)
        if data is None:
            return None

        result = data.get("result")
        if not isinstance(result, dict):
            return None

        contest = result.get("contest") if isinstance(result.get("contest"), dict) else {}
        contest_name = str(contest.get("name", f"Contest {contest_id}"))
        phase = str(contest.get("phase", "UNKNOWN"))

        raw_rows = result.get("rows")
        if not isinstance(raw_rows, list):
            raw_rows = []

        rows: list[CFStandingRow] = []
        for row in raw_rows:
            if not isinstance(row, dict):
                continue

            party = row.get("party") if isinstance(row.get("party"), dict) else {}
            members = party.get("members") if isinstance(party.get("members"), list) else []
            parsed_handles = tuple(
                member["handle"]
                for member in members
                if isinstance(member, dict) and isinstance(member.get("handle"), str)
            )
            if not parsed_handles:
                continue

            participant_type = str(party.get("participantType", "UNKNOWN"))
            rank = int(row.get("rank", 0) or 0)
            points = float(row.get("points", 0.0) or 0.0)
            penalty = int(row.get("penalty", 0) or 0)
            is_official = participant_type == "CONTESTANT"

            rows.append(
                CFStandingRow(
                    rank=rank,
                    points=points,
                    penalty=penalty,
                    participant_type=participant_type,
                    handles=parsed_handles,
                    is_official=is_official,
                )
            )

        return CFStandingsPage(
            contest_id=contest_id,
            contest_name=contest_name,
            phase=phase,
            rows=rows,
        )

    async def get_contest_rating_changes(self, contest_id: int) -> list[CFContestChange]:
        data = await self._get("contest.ratingChanges", {"contestId": contest_id}, require_auth=True)
        if data is None:
            return []

        rows = data.get("result")
        if not isinstance(rows, list):
            return []

        changes: list[CFContestChange] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            changes.append(
                CFContestChange(
                    contest_id=int(row.get("contestId", 0) or 0),
                    contest_name=str(row.get("contestName", f"Contest {contest_id}")),
                    rank=int(row.get("rank", 0) or 0),
                    old_rating=int(row.get("oldRating", 0) or 0),
                    new_rating=int(row.get("newRating", 0) or 0),
                    handle=row.get("handle") if isinstance(row.get("handle"), str) else None,
                )
            )
        return changes
