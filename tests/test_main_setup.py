from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

if "bs4" not in sys.modules:
    bs4_module = types.ModuleType("bs4")
    bs4_module.BeautifulSoup = object
    sys.modules["bs4"] = bs4_module

import main as main_module


class _FakeSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FailingRepository:
    instances: list["_FailingRepository"] = []

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.closed = False
        self.__class__.instances.append(self)

    async def init(self) -> None:
        raise RuntimeError("repo init failed")

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_setup_services_validates_database_url_before_creating_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_sessions: list[_FakeSession] = []

    def _client_session() -> _FakeSession:
        session = _FakeSession()
        created_sessions.append(session)
        return session

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(main_module.aiohttp, "ClientSession", _client_session)

    with pytest.raises(RuntimeError, match="DATABASE_URL is not set"):
        await main_module._setup_services(SimpleNamespace())  # type: ignore[arg-type]

    assert created_sessions == []


@pytest.mark.asyncio
async def test_setup_services_closes_created_session_when_repo_init_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    _FailingRepository.instances.clear()

    monkeypatch.setenv("DATABASE_URL", "postgresql://unit-test")
    monkeypatch.setattr(main_module.aiohttp, "ClientSession", lambda: session)
    monkeypatch.setattr(main_module, "UserRepository", _FailingRepository)

    bot = SimpleNamespace()
    with pytest.raises(RuntimeError, match="repo init failed"):
        await main_module._setup_services(bot)  # type: ignore[arg-type]

    assert session.closed is True
    assert _FailingRepository.instances[0].closed is True
    assert not hasattr(bot, "http_session")
    assert not hasattr(bot, "user_repo")
