from __future__ import annotations

from typing import Any, Optional

import aiohttp
import pytest

from cogs.verification import VerificationCog
from services.cf_client import CFRequestError, CodeforcesClient


class _FakeResponse:
    def __init__(self, *, status: int, payload: Any, json_error: Optional[Exception] = None) -> None:
        self.status = status
        self._payload = payload
        self._json_error = json_error

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False

    async def json(self, content_type: Any = None) -> Any:
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class _FakeSession:
    def __init__(self, scripted: list[Any]) -> None:
        self._scripted = scripted[:]
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, *, params: dict[str, Any], timeout: Any) -> Any:
        self.calls.append((url, dict(params)))
        if not self._scripted:
            raise RuntimeError("No scripted response left")
        item = self._scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FailingCFClient:
    async def get_user(self, handle: str) -> Any:
        raise CFRequestError(
            endpoint="user.info",
            failure_kind="http",
            requested_url="https://codeforces.com/api/user.info?handles=tourist",
            http_status=502,
        )

    async def get_rating_history(self, handle: str) -> list[Any]:
        return []

    async def get_recent_submissions(self, handle: str, count: int = 500) -> list[Any]:
        return []


class _NotFoundCFClient:
    async def get_user(self, handle: str) -> Any:
        return None

    async def get_rating_history(self, handle: str) -> list[Any]:
        return []

    async def get_recent_submissions(self, handle: str, count: int = 500) -> list[Any]:
        return []


class _FakeCtx:
    def __init__(self) -> None:
        self.guild = type("_Guild", (), {"id": 1})()
        self.author = type("_Author", (), {"id": 10})()
        self.messages: list[str] = []

    async def send(self, content: Optional[str] = None, *, embed: Any = None) -> None:
        if content is not None:
            self.messages.append(content)


@pytest.fixture(autouse=True)
def _single_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CF_MAX_RETRIES", "1")
    monkeypatch.setenv("CACHE_TTL_SECONDS", "0")


@pytest.mark.asyncio
async def test_cf_client_maps_http_error_to_typed_exception() -> None:
    client = CodeforcesClient(
        _FakeSession([_FakeResponse(status=502, payload={"status": "FAILED"})])  # type: ignore[arg-type]
    )

    with pytest.raises(CFRequestError) as raised:
        await client.get_user("tourist")

    error = raised.value
    assert error.endpoint == "user.info"
    assert error.failure_kind == "http"
    assert error.http_status == 502
    assert "user.info?handles=tourist" in error.requested_url


@pytest.mark.asyncio
async def test_cf_client_maps_non_ok_payload_to_typed_exception() -> None:
    payload = {"status": "FAILED", "comment": "limit exceeded"}
    client = CodeforcesClient(
        _FakeSession([_FakeResponse(status=200, payload=payload)])  # type: ignore[arg-type]
    )

    with pytest.raises(CFRequestError) as raised:
        await client.get_rating_history("tourist")

    error = raised.value
    assert error.endpoint == "user.rating"
    assert error.failure_kind == "non_ok"
    assert error.cf_comment == "limit exceeded"
    assert error.http_status is None


@pytest.mark.asyncio
async def test_cf_client_error_includes_rendered_requested_url() -> None:
    client = CodeforcesClient(
        _FakeSession([aiohttp.ClientError("network down")])  # type: ignore[arg-type]
    )

    with pytest.raises(CFRequestError) as raised:
        await client.get_recent_submissions("foo bar", count=7)

    error = raised.value
    assert error.endpoint == "user.status"
    assert error.failure_kind == "network"
    assert error.requested_url.startswith("https://codeforces.com/api/user.status?")
    assert "count=7" in error.requested_url
    assert "from=1" in error.requested_url
    assert "handle=foo+bar" in error.requested_url


@pytest.mark.asyncio
async def test_cf_client_maps_invalid_payload_shape_to_parse_error() -> None:
    client = CodeforcesClient(
        _FakeSession([_FakeResponse(status=200, payload=["not", "an", "object"])])  # type: ignore[arg-type]
    )

    with pytest.raises(CFRequestError) as raised:
        await client.get_rating_history("tourist")

    error = raised.value
    assert error.endpoint == "user.rating"
    assert error.failure_kind == "parse"
    assert "user.rating?handle=tourist" in error.requested_url


@pytest.mark.asyncio
async def test_verify_command_surfaces_typed_error_context() -> None:
    ctx = _FakeCtx()
    cog = VerificationCog(
        cf_client=_FailingCFClient(),  # type: ignore[arg-type]
        role_assigner=object(),  # type: ignore[arg-type]
        repo=object(),  # type: ignore[arg-type]
        config_service=object(),  # type: ignore[arg-type]
        reminder_service=None,
    )

    await cog.verify.callback(cog, ctx, "tourist")

    assert len(ctx.messages) == 1
    assert "endpoint user.info" in ctx.messages[0]
    assert "status 502" in ctx.messages[0]


@pytest.mark.asyncio
async def test_verify_command_handles_not_found_distinct_from_transient_failures() -> None:
    ctx = _FakeCtx()
    cog = VerificationCog(
        cf_client=_NotFoundCFClient(),  # type: ignore[arg-type]
        role_assigner=object(),  # type: ignore[arg-type]
        repo=object(),  # type: ignore[arg-type]
        config_service=object(),  # type: ignore[arg-type]
        reminder_service=None,
    )

    await cog.verify.callback(cog, ctx, "ghost_handle")

    assert len(ctx.messages) == 1
    assert "Could not find Codeforces handle" in ctx.messages[0]
