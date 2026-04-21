from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from db.repository import UserRepository, WatchJob


@dataclass
class _State:
    linked: dict[tuple[int, int], dict[str, Any]]
    watch: dict[tuple[int, int, int], dict[str, Any]]


class FakeConn:
    def __init__(self, state: _State) -> None:
        self.state = state

    async def execute(self, query: str, *args: Any) -> str:
        q = " ".join(query.split()).lower()
        now = datetime.now(tz=UTC)

        if "insert into linked_accounts" in q:
            guild_id, discord_user_id, cf_handle = int(args[0]), int(args[1]), str(args[2])
            # unique (guild_id, cf_handle)
            for (g, uid), row in self.state.linked.items():
                if g == guild_id and row["cf_handle"].lower() == cf_handle.lower() and uid != discord_user_id:
                    raise Exception("unique_violation")
            key = (guild_id, discord_user_id)
            created_at = self.state.linked.get(key, {}).get("created_at", now)
            self.state.linked[key] = {
                "guild_id": guild_id,
                "discord_user_id": discord_user_id,
                "cf_handle": cf_handle,
                "created_at": created_at,
                "updated_at": now,
            }
            return "INSERT 0 1"

        if "delete from linked_accounts" in q:
            key = (int(args[0]), int(args[1]))
            existed = key in self.state.linked
            self.state.linked.pop(key, None)
            return "DELETE 1" if existed else "DELETE 0"

        if "insert into watch_jobs" in q:
            key = (int(args[0]), int(args[1]), int(args[2]))
            created_at = self.state.watch.get(key, {}).get("created_at", now)
            self.state.watch[key] = {
                "guild_id": int(args[0]),
                "channel_id": int(args[1]),
                "contest_id": int(args[2]),
                "interval_minutes": int(args[3]),
                "message_id": args[4],
                "server_only": bool(args[5]),
                "show_unofficial": bool(args[6]),
                "enabled": bool(args[7]),
                "created_at": created_at,
                "updated_at": now,
            }
            return "INSERT 0 1"

        if "update watch_jobs set enabled = false" in q:
            key = (int(args[0]), int(args[1]), int(args[2]))
            row = self.state.watch.get(key)
            if row is None:
                return "UPDATE 0"
            row["enabled"] = False
            row["updated_at"] = now
            return "UPDATE 1"

        if "update watch_jobs set message_id" in q:
            key = (int(args[0]), int(args[1]), int(args[2]))
            row = self.state.watch.get(key)
            if row is not None:
                row["message_id"] = int(args[3])
                row["updated_at"] = now
            return "UPDATE 1"

        raise AssertionError(f"Unhandled execute query: {q}")

    async def fetchrow(self, query: str, *args: Any):
        q = " ".join(query.split()).lower()
        if "from linked_accounts" in q:
            return self.state.linked.get((int(args[0]), int(args[1])))
        if "from watch_jobs" in q:
            return self.state.watch.get((int(args[0]), int(args[1]), int(args[2])))
        return None

    async def fetch(self, query: str, *args: Any):
        q = " ".join(query.split()).lower()
        if "from linked_accounts" in q:
            guild_id = int(args[0])
            return [row for row in self.state.linked.values() if row["guild_id"] == guild_id]
        if "from watch_jobs" in q and "where enabled = true" in q:
            return [row for row in self.state.watch.values() if row["enabled"]]
        return []


class FakeAcquire:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    async def __aenter__(self) -> FakeConn:
        return self.conn

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        return None


class FakePool:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.conn)


@pytest.mark.asyncio
async def test_linked_account_persistence_logic() -> None:
    state = _State(linked={}, watch={})
    repo = UserRepository("postgres://unused")
    repo._pool = FakePool(FakeConn(state))  # type: ignore[assignment]

    await repo.upsert_linked_account(1, 10, "tourist")
    linked = await repo.get_linked_account(1, 10)
    assert linked is not None
    assert linked.cf_handle == "tourist"

    all_rows = await repo.get_linked_accounts_for_guild(1)
    assert len(all_rows) == 1

    removed = await repo.remove_linked_account(1, 10)
    assert removed is True
    assert await repo.get_linked_account(1, 10) is None


@pytest.mark.asyncio
async def test_watch_job_persistence_logic() -> None:
    state = _State(linked={}, watch={})
    repo = UserRepository("postgres://unused")
    repo._pool = FakePool(FakeConn(state))  # type: ignore[assignment]

    job = WatchJob(
        guild_id=1,
        channel_id=2,
        contest_id=3,
        interval_minutes=5,
        message_id=None,
        server_only=True,
        show_unofficial=False,
        enabled=True,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )
    await repo.upsert_watch_job(job)

    fetched = await repo.get_watch_job(1, 2, 3)
    assert fetched is not None
    assert fetched.interval_minutes == 5

    await repo.set_watch_job_message_id(1, 2, 3, 123456)
    fetched = await repo.get_watch_job(1, 2, 3)
    assert fetched is not None
    assert fetched.message_id == 123456

    enabled = await repo.get_enabled_watch_jobs()
    assert len(enabled) == 1

    disabled = await repo.disable_watch_job(1, 2, 3)
    assert disabled is True
    assert await repo.get_enabled_watch_jobs() == []