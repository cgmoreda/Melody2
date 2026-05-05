from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from db.repository import TrackedVoiceInterval, UserRepository


class _FakeRecord:
    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def __getitem__(self, key: str) -> Any:
        return self._values[key]


class _FakeTransaction:
    def __init__(self, conn: "_FakeVoiceConnection") -> None:
        self._conn = conn

    async def __aenter__(self) -> "_FakeTransaction":
        self._conn.transaction_events.append("enter")
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self._conn.transaction_events.append("exit")
        return False


class _FakeVoiceConnection:
    def __init__(self, fetch_results: list[list[dict[str, Any]]] | None = None) -> None:
        self.fetch_results = fetch_results or []
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.fetches: list[tuple[str, tuple[Any, ...]]] = []
        self.transaction_events: list[str] = []

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self)

    async def execute(self, query: str, *args: Any) -> str:
        self.executed.append((" ".join(query.split()), args))
        return "OK"

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetches.append((" ".join(query.split()), args))
        if not self.fetch_results:
            return []
        return self.fetch_results.pop(0)


class _FakeAcquire:
    def __init__(self, conn: _FakeVoiceConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeVoiceConnection:
        return self._conn

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False


class _FakePool:
    def __init__(self, conn: _FakeVoiceConnection) -> None:
        self._conn = conn

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


def _repo_with_conn(conn: _FakeVoiceConnection) -> UserRepository:
    repo = UserRepository("postgresql://unit-test")
    repo._pool = _FakePool(conn)  # type: ignore[assignment]
    return repo


@pytest.mark.asyncio
async def test_tracked_voice_totals_merge_overlapping_rows_within_last_five_hours() -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    five_hours_ago = now - timedelta(hours=5)
    conn = _FakeVoiceConnection(
        fetch_results=[
            [
                {"discord_id": 10, "start_ts": five_hours_ago, "end_ts": now},
                {"discord_id": 10, "start_ts": now - timedelta(hours=4), "end_ts": now},
            ]
        ]
    )
    repo = _repo_with_conn(conn)

    totals = await repo.get_tracked_voice_totals(1, now=now, since=five_hours_ago)

    assert totals == {10: 5 * 3600.0}


@pytest.mark.asyncio
async def test_tracked_voice_totals_all_time_accepts_asyncpg_record_like_rows() -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    conn = _FakeVoiceConnection(
        fetch_results=[
            [
                _FakeRecord(
                    {
                        "discord_id": 10,
                        "start_ts": now - timedelta(hours=5),
                        "end_ts": now,
                    }
                ),
                _FakeRecord(
                    {
                        "discord_id": 10,
                        "start_ts": now - timedelta(hours=4),
                        "end_ts": now,
                    }
                ),
            ]
        ]
    )
    repo = _repo_with_conn(conn)

    totals = await repo.get_tracked_voice_totals(1, now=now)

    assert totals == {10: 5 * 3600.0}


@pytest.mark.asyncio
async def test_tracked_voice_summary_uses_merged_intervals_for_each_window() -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    week_since = now - timedelta(days=7)
    month_since = now - timedelta(days=30)
    conn = _FakeVoiceConnection(
        fetch_results=[
            [
                {"discord_id": 10, "start_ts": now - timedelta(hours=6), "end_ts": now},
                {"discord_id": 10, "start_ts": now - timedelta(hours=2), "end_ts": now},
            ],
            [
                {"discord_id": 10, "start_ts": now - timedelta(hours=5), "end_ts": now},
                {"discord_id": 10, "start_ts": now - timedelta(hours=1), "end_ts": now},
            ],
            [
                {"discord_id": 10, "start_ts": now - timedelta(hours=6), "end_ts": now},
                {"discord_id": 10, "start_ts": now - timedelta(hours=2), "end_ts": now},
            ],
        ]
    )
    repo = _repo_with_conn(conn)

    summary = await repo.get_tracked_voice_summary(
        1,
        now=now,
        week_since=week_since,
        month_since=month_since,
    )

    assert summary == {
        10: {
            "week": 5 * 3600.0,
            "month": 6 * 3600.0,
            "all_time": 6 * 3600.0,
        }
    }


@pytest.mark.asyncio
async def test_tracked_voice_intervals_query_returns_clipped_intervals() -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    since = now - timedelta(days=7)
    conn = _FakeVoiceConnection(
        fetch_results=[
            [
                {"discord_id": 10, "start_ts": since, "end_ts": now},
                {
                    "discord_id": 20,
                    "start_ts": now - timedelta(hours=2),
                    "end_ts": now - timedelta(hours=1),
                },
            ]
        ]
    )
    repo = _repo_with_conn(conn)

    intervals = await repo.get_tracked_voice_intervals(1, since=since, now=now)

    assert intervals == [
        TrackedVoiceInterval(discord_id=10, start_ts=since, end_ts=now),
        TrackedVoiceInterval(
            discord_id=20,
            start_ts=now - timedelta(hours=2),
            end_ts=now - timedelta(hours=1),
        ),
    ]
    query, args = conn.fetches[0]
    assert "GREATEST(started_at, $2) AS start_ts" in query
    assert "LEAST(COALESCE(ended_at, $3), $3) AS end_ts" in query
    assert "COALESCE(ended_at, $3) > $2" in query
    assert args == (1, since, now)


@pytest.mark.asyncio
async def test_start_voice_session_closes_existing_open_session_before_insert() -> None:
    started_at = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    conn = _FakeVoiceConnection()
    repo = _repo_with_conn(conn)

    await repo.start_voice_session(
        guild_id=1,
        discord_id=10,
        channel_id=20,
        channel_name="Solo Room A",
        is_tracked=True,
        started_at=started_at,
    )

    statements = [statement for statement, _ in conn.executed]
    assert conn.transaction_events == ["enter", "exit"]
    assert "SELECT pg_advisory_xact_lock($1)" in statements[0]
    assert "UPDATE voice_sessions SET ended_at = GREATEST(started_at, $3)" in statements[1]
    assert "INSERT INTO voice_sessions" in statements[2]
    assert conn.executed[1][1] == (1, 10, started_at)
