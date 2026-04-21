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


class CodeforcesClientBase(abc.ABC):
    """Abstraction for Codeforces API access."""

    @abc.abstractmethod
    async def get_user(self, handle: str) -> Optional[CFUserInfo]:
        """Return user info or ``None`` if the handle does not exist."""


class CodeforcesClient(CodeforcesClientBase):
    """Concrete Codeforces API client backed by ``aiohttp``."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def get_user(self, handle: str) -> Optional[CFUserInfo]:
        url = f"{CF_API_BASE}/user.info"
        params = {"handles": handle, "_": int(time.time())}

        try:
            async with self._session.get(
                url,
                params=params,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.warning("CF API returned status %s for handle %s", resp.status, handle)
                    return None

                data = await resp.json()
        except (aiohttp.ClientError, TimeoutError) as exc:
            logger.error("CF API request failed: %s", exc)
            return None

        if data.get("status") != "OK" or not data.get("result"):
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
