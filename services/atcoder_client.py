from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Optional

import aiohttp

from services.contest_reminder import Contest, ContestProvider

logger = logging.getLogger(__name__)

ATCODER_CONTESTS_API = "https://kenkoooo.com/atcoder/resources/contests.json"


class AtCoderProvider:
    """Fetches upcoming contests from the AtCoder Problems API (kenkoooo)."""

    @property
    def platform(self) -> str:
        return "atcoder"

    async def fetch_upcoming(self, session: aiohttp.ClientSession) -> Optional[list[Contest]]:
        delay = 1.0
        now_epoch = int(datetime.now(tz=UTC).timestamp())

        for attempt in range(1, 4):
            try:
                async with session.get(
                    ATCODER_CONTESTS_API,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as response:
                    if response.status != 200:
                        raise RuntimeError(f"AtCoder contests API returned HTTP {response.status}")
                    payload = await response.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError, RuntimeError) as exc:
                logger.warning("AtCoder contests API attempt %d failed: %s", attempt, exc)
                if attempt < 3:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                return None

            if not isinstance(payload, list):
                logger.warning("AtCoder contests API returned unexpected format")
                return None

            contests: list[Contest] = []
            for row in payload:
                contest_id = row.get("id")
                title = row.get("title")
                start_epoch = row.get("start_epoch_second")
                rate_change = row.get("rate_change", "-")
                if (
                    not isinstance(contest_id, str)
                    or not isinstance(title, str)
                    or not isinstance(start_epoch, int)
                ):
                    continue

                # Only include future contests with rated participation.
                if start_epoch <= now_epoch:
                    continue
                if rate_change == "-":
                    continue

                contests.append(
                    Contest(
                        platform=self.platform,
                        contest_id=contest_id,
                        name=title,
                        start_time_seconds=start_epoch,
                    )
                )

            contests.sort(key=lambda c: c.start_time_seconds)
            return contests

        return None


# Re-export for convenience.
__all__ = ["AtCoderProvider", "ContestProvider"]
