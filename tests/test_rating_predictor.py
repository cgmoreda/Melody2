from services.cf_client import CFStandingRow
from services.rating_predictor import RatingPredictor


def test_predictor_orders_by_rank_and_returns_deltas() -> None:
    predictor = RatingPredictor()
    rows = [
        CFStandingRow(
            rank=1,
            points=300.0,
            penalty=10,
            participant_type="CONTESTANT",
            handles=("alice",),
            is_official=True,
        ),
        CFStandingRow(
            rank=2,
            points=250.0,
            penalty=20,
            participant_type="CONTESTANT",
            handles=("bob",),
            is_official=True,
        ),
    ]
    ratings = {"alice": 1800, "bob": 1700}

    predictions = predictor.predict(rows, ratings, include_unofficial=False)

    assert len(predictions) == 2
    assert predictions[0].handle == "alice"
    assert predictions[1].handle == "bob"
    assert predictions[0].delta >= predictions[1].delta


def test_predictor_marks_unofficial_when_excluded() -> None:
    predictor = RatingPredictor()
    rows = [
        CFStandingRow(
            rank=10,
            points=50.0,
            penalty=100,
            participant_type="VIRTUAL",
            handles=("charlie",),
            is_official=False,
        )
    ]

    included = predictor.predict(rows, {"charlie": 1900}, include_unofficial=True)
    assert len(included) == 1
    assert included[0].note == "Unofficial participant"

    excluded = predictor.predict(rows, {"charlie": 1900}, include_unofficial=False)
    assert excluded == []