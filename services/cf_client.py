from __future__ import annotations

import abc
import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Literal, Optional
from urllib.parse import urlencode

import aiohttp

logger = logging.getLogger(__name__)

CF_API_BASE = "https://codeforces.com/api"
FailureKind = Literal["timeout", "network", "http", "non_ok", "parse"]
SENSITIVE_QUERY_MARKERS: tuple[str, ...] = (
    "token",
    "secret",
    "signature",
    "sig",
    "apikey",
    "api_key",
    "password",
    "auth",
)


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
    contest_id: Optional[int]
    problem_index: Optional[str]


@dataclass(frozen=True, slots=True)
class CFRequestError(Exception):
    endpoint: str
    failure_kind: FailureKind
    requested_url: str
    http_status: Optional[int] = None
    cf_comment: Optional[str] = None
    detail: Optional[str] = None

    @property
    def is_not_found(self) -> bool:
        if self.failure_kind != "non_ok" or self.cf_comment is None:
            return False
        return "not found" in self.cf_comment.lower()

    def __str__(self) -> str:
        status_part = f", status={self.http_status}" if self.http_status is not None else ""
        comment_part = f", comment={self.cf_comment}" if self.cf_comment is not None else ""
        detail_part = f", detail={self.detail}" if self.detail is not None else ""
        return (
            f"CF request failed endpoint={self.endpoint}, kind={self.failure_kind}{status_part}"
            f"{comment_part}, url={self.requested_url}{detail_part}"
        )


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

class CodeforcesClient(CodeforcesClientBase):
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._cache_ttl_seconds = max(0, int(os.getenv("CACHE_TTL_SECONDS", "60")))
        self._timeout_seconds = max(5, int(os.getenv("REQUEST_TIMEOUT_SECONDS", "20")))
        self._max_retries = max(1, int(os.getenv("CF_MAX_RETRIES", "3")))
        self._cache: dict[str, tuple[float, dict]] = {}

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

    @staticmethod
    def _is_sensitive_param(param_name: str) -> bool:
        lowered = param_name.lower()
        return any(marker in lowered for marker in SENSITIVE_QUERY_MARKERS)

    def _build_requested_url(self, endpoint: str, params: dict[str, object]) -> str:
        safe_items: list[tuple[str, str]] = []
        for key, value in sorted(params.items()):
            if self._is_sensitive_param(key):
                safe_items.append((key, "<redacted>"))
                continue
            safe_items.append((key, str(value)))
        query = urlencode(safe_items)
        base = f"{CF_API_BASE}/{endpoint}"
        return f"{base}?{query}" if query else base

    @staticmethod
    def _is_retryable_non_ok_comment(comment: Optional[str]) -> bool:
        if comment is None:
            return False
        lowered = comment.lower()
        return "limit" in lowered or "busy" in lowered or "temporarily unavailable" in lowered

    async def _get(self, endpoint: str, params: dict[str, object]) -> dict:
        request_params = dict(params)
        cache_key = f"{endpoint}?{urlencode(sorted((k, str(v)) for k, v in request_params.items()))}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        url = f"{CF_API_BASE}/{endpoint}"
        requested_url = self._build_requested_url(endpoint, request_params)
        delay = 0.7
        last_error: Optional[CFRequestError] = None
        for attempt in range(1, self._max_retries + 1):
            try:
                async with self._session.get(
                    url,
                    params=request_params,
                    timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
                ) as response:
                    if response.status != 200:
                        last_error = CFRequestError(
                            endpoint=endpoint,
                            failure_kind="http",
                            requested_url=requested_url,
                            http_status=response.status,
                        )
                        if 500 <= response.status < 600 and attempt < self._max_retries:
                            await asyncio.sleep(delay)
                            delay *= 2
                            continue
                        logger.warning("%s", last_error)
                        raise last_error
                    try:
                        payload = await response.json(content_type=None)
                    except ValueError as exc:
                        last_error = CFRequestError(
                            endpoint=endpoint,
                            failure_kind="parse",
                            requested_url=requested_url,
                            http_status=response.status,
                            detail=str(exc),
                        )
                        logger.warning("%s", last_error)
                        raise last_error
            except asyncio.TimeoutError:
                last_error = CFRequestError(
                    endpoint=endpoint,
                    failure_kind="timeout",
                    requested_url=requested_url,
                    detail=f"timeout>{self._timeout_seconds}s",
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                logger.warning("%s", last_error)
                raise last_error
            except aiohttp.ClientError as exc:
                last_error = CFRequestError(
                    endpoint=endpoint,
                    failure_kind="network",
                    requested_url=requested_url,
                    detail=str(exc),
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                logger.warning("%s", last_error)
                raise last_error

            if not isinstance(payload, dict):
                last_error = CFRequestError(
                    endpoint=endpoint,
                    failure_kind="parse",
                    requested_url=requested_url,
                    detail=f"unexpected payload type: {type(payload).__name__}",
                )
                logger.warning("%s", last_error)
                raise last_error
            if payload.get("status") != "OK":
                comment = payload.get("comment")
                cf_comment = comment if isinstance(comment, str) else None
                last_error = CFRequestError(
                    endpoint=endpoint,
                    failure_kind="non_ok",
                    requested_url=requested_url,
                    cf_comment=cf_comment,
                )
                if attempt < self._max_retries and self._is_retryable_non_ok_comment(cf_comment):
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                logger.warning("%s", last_error)
                raise last_error

            self._cache_set(cache_key, payload)
            return payload

        if last_error is None:
            last_error = CFRequestError(
                endpoint=endpoint,
                failure_kind="network",
                requested_url=requested_url,
                detail="exhausted retries",
            )
        raise last_error

    async def get_user(self, handle: str) -> Optional[CFUserInfo]:
        try:
            data = await self._get("user.info", {"handles": handle})
        except CFRequestError as exc:
            if exc.is_not_found:
                return None
            raise

        result = data.get("result")
        if not isinstance(result, list) or not result or not isinstance(result[0], dict):
            raise CFRequestError(
                endpoint="user.info",
                failure_kind="parse",
                requested_url=self._build_requested_url("user.info", {"handles": handle}),
                detail="result payload is missing or malformed",
            )
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
            rows = data.get("result")
            if not isinstance(rows, list):
                raise CFRequestError(
                    endpoint="user.info",
                    failure_kind="parse",
                    requested_url=self._build_requested_url("user.info", {"handles": ";".join(batch)}),
                    detail="result payload is not a list",
                )
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
        rows = data.get("result")
        if not isinstance(rows, list):
            raise CFRequestError(
                endpoint="user.rating",
                failure_kind="parse",
                requested_url=self._build_requested_url("user.rating", {"handle": handle}),
                detail="result payload is not a list",
            )
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
        safe_count = max(1, min(count, 5000))
        data = await self._get("user.status", {"handle": handle, "from": 1, "count": safe_count})
        rows = data.get("result")
        if not isinstance(rows, list):
            raise CFRequestError(
                endpoint="user.status",
                failure_kind="parse",
                requested_url=self._build_requested_url(
                    "user.status",
                    {"handle": handle, "from": 1, "count": safe_count},
                ),
                detail="result payload is not a list",
            )

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
                    contest_id=contest_id if isinstance(contest_id, int) else None,
                    problem_index=index if isinstance(index, str) else None,
                )
            )
        return submissions
