import aiohttp
import pytest

from services.cf_client import CodeforcesClient


@pytest.mark.asyncio
async def test_parse_contest_standings_page() -> None:
    async with aiohttp.ClientSession() as session:
        client = CodeforcesClient(session)

        async def fake_get(endpoint: str, params: dict[str, object], *, require_auth: bool = False):
            assert endpoint == "contest.standings"
            assert require_auth is True
            return {
                "status": "OK",
                "result": {
                    "contest": {"id": 1000, "name": "Test Contest", "phase": "CODING"},
                    "rows": [
                        {
                            "rank": 1,
                            "points": 123.5,
                            "penalty": 10,
                            "party": {
                                "participantType": "CONTESTANT",
                                "members": [{"handle": "tourist"}],
                            },
                        }
                    ],
                },
            }

        client._get = fake_get  # type: ignore[method-assign]
        page = await client.get_contest_standings_page(1000, 1, 50, show_unofficial=False)

    assert page is not None
    assert page.contest_id == 1000
    assert page.contest_name == "Test Contest"
    assert page.phase == "CODING"
    assert len(page.rows) == 1
    row = page.rows[0]
    assert row.rank == 1
    assert row.points == 123.5
    assert row.penalty == 10
    assert row.handles == ("tourist",)
    assert row.participant_type == "CONTESTANT"
    assert row.is_official is True