from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from db.repository import UserRepositoryBase
from services.cf_client import CFContestChange, CFStandingRow, CFStandingsPage, CodeforcesClient
from services.rating_predictor import ParticipantPrediction, RatingPredictor


@dataclass(frozen=True, slots=True)
class ContestPredictionResult:
    contest_id: int
    contest_name: str
    phase: str
    predictions: list[ParticipantPrediction]


class ContestPredictionService:
    def __init__(self, cf: CodeforcesClient, repo: UserRepositoryBase, predictor: RatingPredictor) -> None:
        self._cf = cf
        self._repo = repo
        self._predictor = predictor

    async def build_predictions(
        self,
        *,
        contest_id: int,
        guild_id: Optional[int],
        handles_filter: Optional[set[str]],
        server_only: bool,
        show_unofficial: bool,
    ) -> Optional[ContestPredictionResult]:
        standings = await self._fetch_all_standings(contest_id, show_unofficial=show_unofficial)
        if standings is None:
            return None

        rows = standings.rows
        if handles_filter:
            lowered = {h.lower() for h in handles_filter}
            rows = [row for row in rows if row.handles and row.handles[0].lower() in lowered]

        verified_handle_set: set[str] = set()
        if guild_id is not None:
            verified_users = await self._repo.get_all(guild_id)
            verified_handle_set = {entry.cf_handle.lower() for entry in verified_users}

        if server_only:
            rows = [row for row in rows if row.handles and row.handles[0].lower() in verified_handle_set]

        standings_handles = [row.handles[0] for row in rows if row.handles]
        users = await self._cf.get_users(standings_handles)
        ratings = {handle: info.rating for handle, info in users.items()}

        predictions = self._predictor.predict(rows, ratings, include_unofficial=show_unofficial)
        if server_only and verified_handle_set:
            predictions = [row for row in predictions if row.handle.lower() in verified_handle_set]

        predictions.sort(key=lambda row: (row.rank, row.handle.lower()))

        return ContestPredictionResult(
            contest_id=contest_id,
            contest_name=standings.contest_name,
            phase=standings.phase,
            predictions=predictions,
        )

    async def compare_with_official(
        self,
        *,
        contest_id: int,
        predictions: list[ParticipantPrediction],
    ) -> dict[str, float]:
        official_changes = await self._cf.get_contest_rating_changes(contest_id)
        official_map: dict[str, int] = {}
        for row in official_changes:
            if row.handle is None:
                continue
            official_map[row.handle.lower()] = row.new_rating - row.old_rating
        return self._predictor.compare_with_official(predictions, official_map)

    async def _fetch_all_standings(self, contest_id: int, *, show_unofficial: bool) -> Optional[CFStandingsPage]:
        all_rows: list[CFStandingRow] = []
        start = 1
        contest_name = f"Contest {contest_id}"
        phase = "UNKNOWN"

        while True:
            page = await self._cf.get_contest_standings_page(
                contest_id,
                start,
                self._cf.standings_page_size,
                show_unofficial=show_unofficial,
            )
            if page is None:
                return None

            contest_name = page.contest_name
            phase = page.phase
            all_rows.extend(page.rows)

            if len(page.rows) < self._cf.standings_page_size:
                break
            start += self._cf.standings_page_size

        return CFStandingsPage(
            contest_id=contest_id,
            contest_name=contest_name,
            phase=phase,
            rows=all_rows,
        )
