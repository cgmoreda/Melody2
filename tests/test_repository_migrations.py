from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Optional

import pytest

import db.repository as repository_module
from db.repository import UserRepository


@dataclass
class _FakeConnection:
    schema_version: Optional[int] = None
    existing_constraints: set[str] = field(default_factory=set)
    executed_statements: list[str] = field(default_factory=list)
    schema_version_updates: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.existing_constraints = {name.lower() for name in self.existing_constraints}

    def transaction(self) -> "_FakeTransaction":
        return _FakeTransaction()

    async def execute(self, query: str, *args: Any) -> str:
        normalized = " ".join(query.split())
        self.executed_statements.append(normalized)
        normalized_lower = normalized.lower()

        if "insert into schema_version" in normalized_lower:
            updated_version = int(args[0])
            self.schema_version = updated_version
            self.schema_version_updates.append(updated_version)

        match = re.search(r"add constraint\s+([a-zA-Z0-9_]+)", normalized_lower)
        if match:
            self.existing_constraints.add(match.group(1))

        return "OK"

    async def fetchval(self, query: str, *args: Any) -> Any:
        normalized = " ".join(query.split()).lower()
        if "select version from schema_version" in normalized:
            return self.schema_version
        if "from pg_constraint" in normalized:
            constraint_name = str(args[1]).lower()
            return 1 if constraint_name in self.existing_constraints else None
        return None


class _FakeTransaction:
    async def __aenter__(self) -> "_FakeTransaction":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False


class _FakeAcquire:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConnection:
        return self._conn

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False


class _FakePool:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)

    async def close(self) -> None:
        return None


def _contains_statement(statements: list[str], snippet: str) -> bool:
    needle = " ".join(snippet.split()).lower()
    return any(needle in statement.lower() for statement in statements)


def _statement_index(statements: list[str], snippet: str) -> int:
    needle = " ".join(snippet.split()).lower()
    for index, statement in enumerate(statements):
        if needle in statement.lower():
            return index
    raise AssertionError(f"Expected to find SQL snippet: {snippet}")


async def _run_init_with_fake_conn(
    monkeypatch: pytest.MonkeyPatch,
    conn: _FakeConnection,
) -> UserRepository:
    fake_pool = _FakePool(conn)

    async def _fake_create_pool(_: str) -> _FakePool:
        return fake_pool

    monkeypatch.setattr(repository_module.asyncpg, "create_pool", _fake_create_pool)
    repo = UserRepository("postgresql://unit-test")
    await repo.init()
    return repo


@pytest.mark.asyncio
async def test_init_new_database_runs_all_migrations_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _FakeConnection()
    repo = await _run_init_with_fake_conn(monkeypatch, conn)

    assert conn.schema_version == repo._LATEST_SCHEMA_VERSION
    assert conn.schema_version_updates == [0, 1, 2, 3, 4]

    assert _contains_statement(conn.executed_statements, "CREATE TABLE IF NOT EXISTS schema_version")
    assert _contains_statement(conn.executed_statements, "CREATE TABLE IF NOT EXISTS verified_users")
    assert _contains_statement(
        conn.executed_statements,
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_verified_users_guild_cf_handle_ci",
    )

    expected_constraints = {
        "fk_gym_problem_tags_contest",
        "fk_gym_problem_ratings_contest",
        "fk_gym_quality_ratings_contest",
        "fk_gym_participation_cache_contest",
    }
    assert expected_constraints.issubset(conn.existing_constraints)


@pytest.mark.asyncio
async def test_init_upgrade_from_version_one_skips_base_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _FakeConnection(schema_version=1)
    repo = await _run_init_with_fake_conn(monkeypatch, conn)

    assert conn.schema_version == repo._LATEST_SCHEMA_VERSION
    assert conn.schema_version_updates == [2, 3, 4]
    assert not _contains_statement(conn.executed_statements, "CREATE TABLE IF NOT EXISTS verified_users")
    assert _contains_statement(
        conn.executed_statements,
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_verified_users_guild_cf_handle_ci",
    )


@pytest.mark.asyncio
async def test_integrity_cleanup_runs_before_constraints_and_unique_index(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _FakeConnection(schema_version=2)
    await _run_init_with_fake_conn(monkeypatch, conn)

    dedupe_idx = _statement_index(conn.executed_statements, "WITH ranked AS")
    unique_idx = _statement_index(
        conn.executed_statements,
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_verified_users_guild_cf_handle_ci",
    )
    assert dedupe_idx < unique_idx

    fk_pairs = [
        ("gym_problem_tags", "fk_gym_problem_tags_contest"),
        ("gym_problem_ratings", "fk_gym_problem_ratings_contest"),
        ("gym_quality_ratings", "fk_gym_quality_ratings_contest"),
        ("gym_participation_cache", "fk_gym_participation_cache_contest"),
    ]
    for table_name, constraint_name in fk_pairs:
        cleanup_idx = _statement_index(conn.executed_statements, f"DELETE FROM {table_name} child")
        alter_idx = _statement_index(
            conn.executed_statements,
            f"ALTER TABLE {table_name} ADD CONSTRAINT {constraint_name}",
        )
        assert cleanup_idx < alter_idx
