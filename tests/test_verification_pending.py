from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Optional

import pytest

import cogs.verification as verification_module
from cogs.verification import VerificationCog
from db.repository import PendingVerification, VerifiedUser
from services.cf_client import CFContestChange, CFSubmission, CFUserInfo


class _FakeRepo:
    def __init__(self) -> None:
        self.pending: dict[tuple[int, int], PendingVerification] = {}
        self.verified: list[VerifiedUser] = []

    async def upsert_pending_verification(
        self,
        *,
        guild_id: int,
        discord_id: int,
        cf_handle: str,
        verification_code: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> None:
        self.pending[(guild_id, discord_id)] = PendingVerification(
            guild_id=guild_id,
            discord_id=discord_id,
            cf_handle=cf_handle,
            verification_code=verification_code,
            created_at=created_at,
            expires_at=expires_at,
        )

    async def get_pending_verification(self, guild_id: int, discord_id: int) -> Optional[PendingVerification]:
        return self.pending.get((guild_id, discord_id))

    async def delete_pending_verification(self, guild_id: int, discord_id: int) -> bool:
        return self.pending.pop((guild_id, discord_id), None) is not None

    async def upsert(self, user: VerifiedUser) -> None:
        self.verified.append(user)


class _FakeCFClient:
    def __init__(self, users: dict[str, CFUserInfo]) -> None:
        self._users = {key.lower(): value for key, value in users.items()}

    async def get_user(self, handle: str) -> Optional[CFUserInfo]:
        return self._users.get(handle.lower())

    async def get_rating_history(self, handle: str) -> list[CFContestChange]:
        return []

    async def get_recent_submissions(self, handle: str, count: int = 500) -> list[CFSubmission]:
        return []


class _FakeRole:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeRoleAssigner:
    def __init__(self) -> None:
        self.apply_calls: list[tuple[int, int, int]] = []

    def role_for(self, rating: int) -> None:
        return None

    async def apply(self, member: Any, guild: Any, rating: int) -> _FakeRole:
        self.apply_calls.append((member.id, guild.id, rating))
        return _FakeRole("Pupil")


class _FakeGuild:
    def __init__(self, guild_id: int) -> None:
        self.id = guild_id


class _FakeMember:
    def __init__(self, member_id: int) -> None:
        self.id = member_id
        self.mention = f"<@{member_id}>"


class _FakeContext:
    def __init__(self, guild: _FakeGuild, author: _FakeMember) -> None:
        self.guild = guild
        self.author = author
        self.messages: list[str] = []
        self.embeds: list[Any] = []

    async def send(self, content: Optional[str] = None, *, embed: Any = None) -> None:
        if content is not None:
            self.messages.append(content)
        if embed is not None:
            self.embeds.append(embed)


def _build_user(*, handle: str, first_name: Optional[str], max_rating: int = 1900) -> CFUserInfo:
    return CFUserInfo(
        handle=handle,
        first_name=first_name,
        rating=max_rating,
        max_rating=max_rating,
        rank="candidate master",
        max_rank="candidate master",
        country=None,
        city=None,
        organization=None,
        contribution=0,
        friend_of_count=0,
        avatar_url=None,
        title_photo_url=None,
    )


@pytest.fixture(autouse=True)
def _patch_member_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verification_module.discord, "Member", _FakeMember)


@pytest.mark.asyncio
async def test_pending_verification_survives_across_cog_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verification_module, "_generate_code", lambda length=6: "CF-VERIFY-fixed")

    repo = _FakeRepo()
    guild = _FakeGuild(101)
    member = _FakeMember(202)
    ctx = _FakeContext(guild, member)

    cog_one = VerificationCog(
        cf_client=_FakeCFClient({"tourist": _build_user(handle="tourist", first_name=None)}),  # type: ignore[arg-type]
        role_assigner=_FakeRoleAssigner(),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=object(),  # type: ignore[arg-type]
        reminder_service=None,
    )
    await cog_one.verify.callback(cog_one, ctx, "tourist")

    pending = await repo.get_pending_verification(guild.id, member.id)
    assert pending is not None
    assert pending.verification_code == "CF-VERIFY-fixed"

    cog_two_roles = _FakeRoleAssigner()
    cog_two = VerificationCog(
        cf_client=_FakeCFClient({"tourist": _build_user(handle="tourist", first_name="CF-VERIFY-fixed")}),  # type: ignore[arg-type]
        role_assigner=cog_two_roles,  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=object(),  # type: ignore[arg-type]
        reminder_service=None,
    )
    await cog_two.confirm.callback(cog_two, ctx)

    assert await repo.get_pending_verification(guild.id, member.id) is None
    assert len(repo.verified) == 1
    assert repo.verified[0].cf_handle == "tourist"
    assert cog_two_roles.apply_calls == [(member.id, guild.id, 1900)]
    assert ctx.embeds[-1].title == "Verification Successful"


@pytest.mark.asyncio
async def test_confirm_expired_pending_verification_clears_row() -> None:
    repo = _FakeRepo()
    guild = _FakeGuild(11)
    member = _FakeMember(22)
    ctx = _FakeContext(guild, member)

    now = datetime.now(tz=UTC)
    await repo.upsert_pending_verification(
        guild_id=guild.id,
        discord_id=member.id,
        cf_handle="expired_handle",
        verification_code="CF-VERIFY-old",
        created_at=now - timedelta(minutes=30),
        expires_at=now - timedelta(minutes=1),
    )

    cog = VerificationCog(
        cf_client=_FakeCFClient({"expired_handle": _build_user(handle="expired_handle", first_name="CF-VERIFY-old")}),  # type: ignore[arg-type]
        role_assigner=_FakeRoleAssigner(),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=object(),  # type: ignore[arg-type]
        reminder_service=None,
    )

    await cog.confirm.callback(cog, ctx)

    assert any("expired" in message.lower() for message in ctx.messages)
    assert await repo.get_pending_verification(guild.id, member.id) is None
    assert repo.verified == []


@pytest.mark.asyncio
async def test_confirm_success_deletes_pending_row() -> None:
    repo = _FakeRepo()
    guild = _FakeGuild(303)
    member = _FakeMember(404)
    ctx = _FakeContext(guild, member)

    now = datetime.now(tz=UTC)
    await repo.upsert_pending_verification(
        guild_id=guild.id,
        discord_id=member.id,
        cf_handle="active_handle",
        verification_code="CF-VERIFY-ok",
        created_at=now,
        expires_at=now + timedelta(minutes=10),
    )

    roles = _FakeRoleAssigner()
    cog = VerificationCog(
        cf_client=_FakeCFClient({"active_handle": _build_user(handle="active_handle", first_name="CF-VERIFY-ok", max_rating=2100)}),  # type: ignore[arg-type]
        role_assigner=roles,  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=object(),  # type: ignore[arg-type]
        reminder_service=None,
    )

    await cog.confirm.callback(cog, ctx)

    assert await repo.get_pending_verification(guild.id, member.id) is None
    assert len(repo.verified) == 1
    assert repo.verified[0].cf_handle == "active_handle"
    assert roles.apply_calls == [(member.id, guild.id, 2100)]


@pytest.mark.asyncio
async def test_confirm_code_mismatch_keeps_pending_row_active() -> None:
    repo = _FakeRepo()
    guild = _FakeGuild(700)
    member = _FakeMember(701)
    ctx = _FakeContext(guild, member)

    now = datetime.now(tz=UTC)
    await repo.upsert_pending_verification(
        guild_id=guild.id,
        discord_id=member.id,
        cf_handle="mismatch_handle",
        verification_code="CF-VERIFY-expected",
        created_at=now,
        expires_at=now + timedelta(minutes=10),
    )

    roles = _FakeRoleAssigner()
    cog = VerificationCog(
        cf_client=_FakeCFClient({"mismatch_handle": _build_user(handle="mismatch_handle", first_name="wrong-code")}),  # type: ignore[arg-type]
        role_assigner=roles,  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        config_service=object(),  # type: ignore[arg-type]
        reminder_service=None,
    )

    await cog.confirm.callback(cog, ctx)

    pending = await repo.get_pending_verification(guild.id, member.id)
    assert pending is not None
    assert pending.verification_code == "CF-VERIFY-expected"
    assert repo.verified == []
    assert roles.apply_calls == []
    assert any("First name mismatch" in message for message in ctx.messages)
