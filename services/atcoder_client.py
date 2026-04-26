from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Optional

import aiohttp
from bs4 import BeautifulSoup

from services.contest_reminder import Contest, ContestProvider

logger = logging.getLogger(__name__)

ATCODER_CONTESTS_URL = "https://atcoder.jp/contests/"

class AtCoderProvider:
    """Fetches upcoming contests by scraping AtCoder's official page."""

    @property
    def platform(self) -> str:
        return "atcoder"

    async def fetch_upcoming(self, session: aiohttp.ClientSession) -> Optional[list[Contest]]:
        delay = 1.0

        for attempt in range(1, 4):
            try:
                async with session.get(
                    ATCODER_CONTESTS_URL,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as response:
                    if response.status != 200:
                        raise RuntimeError(f"AtCoder page returned HTTP {response.status}")
                    html = await response.text()
            except (aiohttp.ClientError, TimeoutError, RuntimeError) as exc:
                logger.warning("AtCoder scrape attempt %d failed: %s", attempt, exc)
                if attempt < 3:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                return None

            try:
                soup = BeautifulSoup(html, "html.parser")
                upcoming_rows = soup.select("#contest-table-upcoming table tbody tr")
            except Exception as exc:
                logger.warning("Failed parsing AtCoder HTML: %s", exc)
                return None

            contests: list[Contest] = []
            for row in upcoming_rows:
                try:
                    tds = row.select("td")
                    if len(tds) < 2:
                        continue
                        
                    time_elem = tds[0].select_one("a")
                    if not time_elem:
                        continue
                    time_str = time_elem.text.strip()
                    
                    title_elem = tds[1].select_one("a")
                    if not title_elem:
                        continue
                    title = title_elem.text.strip()
                    href = title_elem.get("href", "")
                    
                    # Extract contest_id from /contests/abc456
                    contest_id = href.split("/")[-1]
                    if not contest_id:
                        continue
                    
                    # Parse time like "2026-04-26 15:00:00+0900"
                    start_dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S%z")
                    start_epoch = int(start_dt.timestamp())
                    
                    contests.append(
                        Contest(
                            platform=self.platform,
                            contest_id=contest_id,
                            name=title,
                            start_time_seconds=start_epoch,
                        )
                    )
                except Exception as exc:
                    logger.debug("Skipped an AtCoder row due to parse error: %s", exc)
                    continue

            contests.sort(key=lambda c: c.start_time_seconds)
            return contests

        return None

__all__ = ["AtCoderProvider"]
