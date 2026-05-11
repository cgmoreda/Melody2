from __future__ import annotations

from typing import Any, Optional

import aiohttp
import pytest

from cogs.verification import VerificationCog
from db.repository import GuildCommandConfig, VerifiedUser
from services.cf_client import CFContestChange, CFRequestError, CodeforcesClient


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


class _RoundChangesRepo:
    async def get_all(self, guild_id: int) -> list[VerifiedUser]:
        return [
            VerifiedUser(discord_id=1, cf_handle="ok_handle", rating=1200, guild_id=guild_id),
            VerifiedUser(discord_id=2, cf_handle="bad_handle", rating=1200, guild_id=guild_id),
        ]


class _RoundChangesConfig:
    async def get(self, guild_id: int) -> GuildCommandConfig:
        return GuildCommandConfig(
            guild_id=guild_id,
            reminder_preview_limit=3,
            roundchanges_max_lines=10,
            voicehours_max_lines=35,
            voice_check_interval_seconds=900,
            voice_confirm_timeout_seconds=180,
        )


class _RoundChangesCF:
    async def get_rating_history(self, handle: str) -> list[CFContestChange]:
        if handle == "bad_handle":
            raise CFRequestError(
                endpoint="user.rating",
                failure_kind="network",
                requested_url="https://codeforces.com/api/user.rating?handle=bad_handle",
            )
        return [
            CFContestChange(
                contest_id=1000,
                contest_name="Round 1000",
                rank=12,
                old_rating=1200,
                new_rating=1300,
                handle=handle,
            )
        ]

    async def get_user(self, handle: str) -> Any:
        return None

    async def get_recent_submissions(self, handle: str, count: int = 500) -> list[Any]:
        return []


class _RoundGuild:
    id = 1

    def get_member(self, discord_id: int) -> Any:
        return None


class _RoundCtx:
    def __init__(self) -> None:
        self.guild = _RoundGuild()
        self.author = type("_Author", (), {"id": 10})()
        self.messages: list[str] = []
        self.embeds: list[Any] = []

    async def send(self, content: Optional[str] = None, *, embed: Any = None) -> None:
        if content is not None:
            self.messages.append(content)
        if embed is not None:
            self.embeds.append(embed)


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


def test_cf_client_invalid_env_values_fall_back_with_warnings(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("CACHE_TTL_SECONDS", "not-an-int")
    monkeypatch.setenv("REQUEST_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("CF_MAX_RETRIES", "bad")

    client = CodeforcesClient(_FakeSession([]))  # type: ignore[arg-type]

    assert client._cache_ttl_seconds == 60
    assert client._timeout_seconds == 5
    assert client._max_retries == 3
    assert "Invalid CACHE_TTL_SECONDS" in caplog.text
    assert "Invalid REQUEST_TIMEOUT_SECONDS" in caplog.text
    assert "Invalid CF_MAX_RETRIES" in caplog.text


def test_cf_client_cache_evicts_least_recent_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CACHE_TTL_SECONDS", "60")
    monkeypatch.setenv("CF_CACHE_MAX_ENTRIES", "2")
    client = CodeforcesClient(_FakeSession([]))  # type: ignore[arg-type]

    client._cache_set("a", {"status": "OK", "result": ["a"]})
    client._cache_set("b", {"status": "OK", "result": ["b"]})
    assert client._cache_get("a") == {"status": "OK", "result": ["a"]}
    client._cache_set("c", {"status": "OK", "result": ["c"]})

    assert list(client._cache) == ["a", "c"]


def test_cf_client_cache_prunes_expired_entries_on_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CACHE_TTL_SECONDS", "60")
    monkeypatch.setenv("CF_CACHE_MAX_ENTRIES", "10")
    client = CodeforcesClient(_FakeSession([]))  # type: ignore[arg-type]
    client._cache["old"] = (0, {"status": "OK"})

    client._cache_set("new", {"status": "OK"})

    assert "old" not in client._cache
    assert "new" in client._cache


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


@pytest.mark.asyncio
async def test_roundchanges_continues_when_one_handle_history_fetch_fails() -> None:
    ctx = _RoundCtx()
    cog = VerificationCog(
        cf_client=_RoundChangesCF(),  # type: ignore[arg-type]
        role_assigner=object(),  # type: ignore[arg-type]
        repo=_RoundChangesRepo(),  # type: ignore[arg-type]
        config_service=_RoundChangesConfig(),  # type: ignore[arg-type]
        reminder_service=None,
    )

    await cog.roundchanges.callback(cog, ctx)

    assert ctx.messages == []
    assert len(ctx.embeds) == 1
    description = ctx.embeds[0].description
    assert "ok_handle" in description
    assert "bad_handle" not in description
    assert "Skipped **1** verified user" in description
