from __future__ import annotations

import math
from dataclasses import dataclass

from services.cf_client import CFStandingRow


@dataclass(frozen=True, slots=True)
class ParticipantPrediction:
    handle: str
    rank: int
    current_rating: int
    seed: float
    needed_rating: int
    delta: int
    new_rating: int
    performance: int
    points: float
    penalty: int
    participant_type: str
    is_official: bool
    note: str


class RatingPredictor:
    """Approximate Codeforces rating predictor for ongoing contests."""

    def predict(
        self,
        standings_rows: list[CFStandingRow],
        ratings_by_handle: dict[str, int],
        *,
        include_unofficial: bool,
    ) -> list[ParticipantPrediction]:
        participants: list[ParticipantPrediction] = []

        contenders: list[tuple[str, CFStandingRow, int]] = []
        for row in standings_rows:
            handle = row.handles[0]
            rating = ratings_by_handle.get(handle.lower(), 0)
            official = row.is_official
            if official and rating > 0:
                contenders.append((handle, row, rating))

        if contenders:
            predicted = self._predict_official(contenders)
        else:
            predicted = {}

        for row in standings_rows:
            handle = row.handles[0]
            if not include_unofficial and not row.is_official:
                continue

            key = handle.lower()
            if key in predicted:
                participants.append(predicted[key])
                continue

            rating = ratings_by_handle.get(key, 0)
            note = "Unofficial participant" if not row.is_official else "Unrated participant"
            participants.append(
                ParticipantPrediction(
                    handle=handle,
                    rank=row.rank,
                    current_rating=rating,
                    seed=0.0,
                    needed_rating=rating,
                    delta=0,
                    new_rating=rating,
                    performance=rating,
                    points=row.points,
                    penalty=row.penalty,
                    participant_type=row.participant_type,
                    is_official=row.is_official,
                    note=note,
                )
            )

        participants.sort(key=lambda item: (item.rank, item.handle.lower()))
        return participants

    def compare_with_official(
        self,
        predictions: list[ParticipantPrediction],
        official_deltas: dict[str, int],
    ) -> dict[str, float]:
        errors: list[int] = []
        exact_matches = 0
        close_matches = 0

        for row in predictions:
            official = official_deltas.get(row.handle.lower())
            if official is None:
                continue
            err = abs(row.delta - official)
            errors.append(err)
            if err == 0:
                exact_matches += 1
            if err <= 10:
                close_matches += 1

        if not errors:
            return {
                "count": 0,
                "mae": 0.0,
                "max_error": 0.0,
                "exact": 0,
                "close": 0,
            }

        return {
            "count": float(len(errors)),
            "mae": sum(errors) / len(errors),
            "max_error": float(max(errors)),
            "exact": float(exact_matches),
            "close": float(close_matches),
        }

    def _predict_official(
        self,
        contenders: list[tuple[str, CFStandingRow, int]],
    ) -> dict[str, ParticipantPrediction]:
        ratings = [rating for _, _, rating in contenders]

        def seed_for_rating(candidate_rating: float) -> float:
            return 1.0 + sum(1.0 / (1.0 + 10.0 ** ((candidate_rating - other) / 400.0)) for other in ratings)

        raw_rows: list[tuple[str, CFStandingRow, int, float, int, int]] = []
        for handle, row, rating in contenders:
            seed = seed_for_rating(float(rating))
            mid_rank = math.sqrt(max(1.0, row.rank) * max(1.0, seed))
            needed_rating = self._invert_seed(mid_rank, ratings)
            delta = int(round((needed_rating - rating) / 2.0))
            performance = needed_rating
            raw_rows.append((handle, row, rating, seed, needed_rating, delta))

        adjusted_deltas = self._apply_corrections([item[5] for item in raw_rows])

        out: dict[str, ParticipantPrediction] = {}
        for (handle, row, rating, seed, needed_rating, _), delta in zip(raw_rows, adjusted_deltas):
            out[handle.lower()] = ParticipantPrediction(
                handle=handle,
                rank=row.rank,
                current_rating=rating,
                seed=seed,
                needed_rating=needed_rating,
                delta=delta,
                new_rating=rating + delta,
                performance=needed_rating,
                points=row.points,
                penalty=row.penalty,
                participant_type=row.participant_type,
                is_official=row.is_official,
                note="Approximation",
            )
        return out

    def _invert_seed(self, target_seed: float, ratings: list[int]) -> int:
        low = -1000
        high = 8000
        for _ in range(30):
            mid = (low + high) / 2
            seed = 1.0 + sum(1.0 / (1.0 + 10.0 ** ((mid - other) / 400.0)) for other in ratings)
            if seed > target_seed:
                low = mid
            else:
                high = mid
        return int(round((low + high) / 2))

    def _apply_corrections(self, deltas: list[int]) -> list[int]:
        if not deltas:
            return []

        corrected = list(deltas)
        n = len(corrected)

        global_shift = int(round(-sum(corrected) / n))
        corrected = [delta + global_shift for delta in corrected]

        top = max(1, int(4 * math.sqrt(n)))
        top_shift = int(round(-sum(sorted(corrected, reverse=True)[:top]) / top))
        corrected = [delta + top_shift for delta in corrected]
        return corrected