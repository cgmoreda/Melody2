# services/cf_client.py
# Codeforces API wrapper — handles user.info lookups.
# Usage: client = CodeforcesClient(session); info = await client.get_user("handle")

from __future__ import annotations

import abc
import logging
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


class CodeforcesClientBase(abc.ABC):
    """Abstraction for Codeforces API access (DIP)."""

    @abc.abstractmethod
    async def get_user(self, handle: str) -> Optional[CFUserInfo]:
        """Return user info or ``None`` if the handle does not exist."""


class CodeforcesClient(CodeforcesClientBase):
    """Concrete Codeforces API client backed by *aiohttp*."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def get_user(self, handle: str) -> Optional[CFUserInfo]:
        import time
        url = f"{CF_API_BASE}/user.info"
        # The '_' parameter with a timestamp ensures CF doesn't return a cached response.
        params = {"handles": handle, "_": int(time.time())}

        try:
            async with self._session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
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
        )
