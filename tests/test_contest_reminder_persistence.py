from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import services.contest_reminder as contest_reminder_module
from services.contest_reminder import Contest, ContestReminderService


class _PersistentReminderRepo:
    def __init__(self) -> None:
        self._sent: set[tuple[int, int, str, str, str]] = set()
        self.has_calls = 0
        self.mark_calls = 0

    async def get_reminder_channels(self) -> list[tuple[int, int]]:
        return []

    async def cleanup_old_sent_contest_reminders(self, before: datetime) -> int:
        return 0

    async def has_sent_contest_reminder(
        self,
        guild_id: int,
        channel_id: int,
        contest_id: str,
        reminder_type: str,
        platform: str = "codeforces",
    ) -> bool:
        self.has_calls += 1
        return (guild_id, channel_id, platform, contest_id, reminder_type) in self._sent

    async def mark_contest_reminder_sent(
        self,
        guild_id: int,
        channel_id: int,
        contest_id: str,
        reminder_type: str,
        sent_at: datetime,
        platform: str = "codeforces",
    ) -> bool:
        self.mark_calls += 1
        key = (guild_id, channel_id, platform, contest_id, reminder_type)
        if key in self._sent:
            return False
        self._sent.add(key)
        return True


class _FakeTextChannel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.messages: list[str] = []

    async def send(self, message: str) -> None:
        await asyncio.sleep(0.02)
        self.messages.append(message)


class _FakeBot:
    def __init__(self, channel: _FakeTextChannel) -> None:
        self._channel = channel

    def get_channel(self, channel_id: int) -> _FakeTextChannel | None:
        if channel_id == self._channel.id:
            return self._channel
        return None


class _FakeProvider:
    def __init__(self, platform: str, contests: list[Contest]) -> None:
        self._platform = platform
        self._contests = contests

    @property
    def platform(self) -> str:
        return self._platform

    async def fetch_upcoming(self, session: Any) -> list[Contest]:
        return list(self._contests)


def _service(repo: _PersistentReminderRepo) -> ContestReminderService:
    return ContestReminderService(
        session=object(),  # type: ignore[arg-type]
        bot=object(),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        poll_seconds=300,
    )


@pytest.mark.asyncio
async def test_concurrent_reminder_attempts_send_once_and_mark_once() -> None:
    repo = _PersistentReminderRepo()
    service = _service(repo)
    channel = _FakeTextChannel(12345)
    contest = Contest(
        platform="codeforces",
        contest_id="2062",
        name="Codeforces Round 2062 (Div. 2)",
        start_time_seconds=int(datetime.now(tz=UTC).timestamp()) + 24 * 3600,
    )

    async def _attempt() -> None:
        await service._maybe_send_reminder(
            guild_id=77,
            channel=channel,  # type: ignore[arg-type]
            contest=contest,
            reminder_type="24h",
            target=timedelta(hours=24),
            window=timedelta(minutes=5),
            message="[Reminder] test starts in 24h",
            until_start=timedelta(hours=24) - timedelta(seconds=30),
        )

    await asyncio.gather(_attempt(), _attempt())

    assert channel.messages == ["[Reminder] test starts in 24h"]
    assert repo.mark_calls == 1
    assert repo.has_calls >= 1


@pytest.mark.asyncio
async def test_persisted_dedupe_survives_restart_like_new_service_instance() -> None:
    repo = _PersistentReminderRepo()
    channel = _FakeTextChannel(54321)
    contest = Contest(
        platform="codeforces",
        contest_id="1888",
        name="Codeforces Round 1888 (Div. 3)",
        start_time_seconds=int(datetime.now(tz=UTC).timestamp()) + 3600,
    )

    service_before_restart = _service(repo)
    await service_before_restart._maybe_send_reminder(
        guild_id=99,
        channel=channel,  # type: ignore[arg-type]
        contest=contest,
        reminder_type="1h",
        target=timedelta(hours=1),
        window=timedelta(minutes=5),
        message="[Reminder] test starts in 1h",
        until_start=timedelta(hours=1) - timedelta(seconds=20),
    )

    service_after_restart = _service(repo)
    await service_after_restart._maybe_send_reminder(
        guild_id=99,
        channel=channel,  # type: ignore[arg-type]
        contest=contest,
        reminder_type="1h",
        target=timedelta(hours=1),
        window=timedelta(minutes=5),
        message="[Reminder] test starts in 1h",
        until_start=timedelta(hours=1) - timedelta(seconds=10),
    )

    assert channel.messages == ["[Reminder] test starts in 1h"]
    assert repo.mark_calls == 1


@pytest.mark.asyncio
async def test_existing_persisted_dedupe_key_skips_send_and_mark() -> None:
    repo = _PersistentReminderRepo()
    repo._sent.add((123, 456, "codeforces", "789", "24h"))
    service = _service(repo)
    channel = _FakeTextChannel(456)
    contest = Contest(
        platform="codeforces",
        contest_id="789",
        name="Codeforces Round 789 (Div. 2)",
        start_time_seconds=int(datetime.now(tz=UTC).timestamp()) + 24 * 3600,
    )

    await service._maybe_send_reminder(
        guild_id=123,
        channel=channel,  # type: ignore[arg-type]
        contest=contest,
        reminder_type="24h",
        target=timedelta(hours=24),
        window=timedelta(minutes=5),
        message="[Reminder] already persisted",
        until_start=timedelta(hours=24) - timedelta(seconds=1),
    )

    assert channel.messages == []
    assert repo.has_calls >= 1
    assert repo.mark_calls == 0


@pytest.mark.asyncio
async def test_atcoder_contest_dedupe_independent_from_codeforces() -> None:
    """Verify that AtCoder and CF contests with identical IDs don't collide."""
    repo = _PersistentReminderRepo()
    service = _service(repo)
    channel = _FakeTextChannel(9999)

    cf_contest = Contest(
        platform="codeforces",
        contest_id="100",
        name="Codeforces Round 100",
        start_time_seconds=int(datetime.now(tz=UTC).timestamp()) + 3600,
    )
    ac_contest = Contest(
        platform="atcoder",
        contest_id="100",
        name="AtCoder Beginner Contest 100",
        start_time_seconds=int(datetime.now(tz=UTC).timestamp()) + 3600,
    )

    await service._maybe_send_reminder(
        guild_id=1,
        channel=channel,  # type: ignore[arg-type]
        contest=cf_contest,
        reminder_type="1h",
        target=timedelta(hours=1),
        window=timedelta(minutes=5),
        message="[Reminder] CF 100 starts in 1h",
        until_start=timedelta(hours=1) - timedelta(seconds=30),
    )
    await service._maybe_send_reminder(
        guild_id=1,
        channel=channel,  # type: ignore[arg-type]
        contest=ac_contest,
        reminder_type="1h",
        target=timedelta(hours=1),
        window=timedelta(minutes=5),
        message="[Reminder] AC 100 starts in 1h",
        until_start=timedelta(hours=1) - timedelta(seconds=30),
    )

    assert channel.messages == [
        "[Reminder] CF 100 starts in 1h",
        "[Reminder] AC 100 starts in 1h",
    ]
    assert repo.mark_calls == 2


@pytest.mark.asyncio
async def test_tick_sends_codeforces_div_reminders_only_and_keeps_atcoder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contest_reminder_module.discord, "TextChannel", _FakeTextChannel)

    repo = _PersistentReminderRepo()
    channel = _FakeTextChannel(2468)
    starts_soon = int((datetime.now(tz=UTC) + timedelta(hours=24) - timedelta(seconds=30)).timestamp())
    cf_div = Contest(
        platform="codeforces",
        contest_id="1",
        name="Codeforces Round 1 (Div. 2)",
        start_time_seconds=starts_soon,
    )
    cf_non_div = Contest(
        platform="codeforces",
        contest_id="2",
        name="Codeforces Kotlin Heroes Practice",
        start_time_seconds=starts_soon,
    )
    atcoder = Contest(
        platform="atcoder",
        contest_id="abc999",
        name="AtCoder Beginner Contest 999",
        start_time_seconds=starts_soon,
    )
    service = ContestReminderService(
        session=object(),  # type: ignore[arg-type]
        bot=_FakeBot(channel),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        providers=[
            _FakeProvider("codeforces", [cf_div, cf_non_div]),
            _FakeProvider("atcoder", [atcoder]),
        ],
        poll_seconds=300,
    )
    service._enabled_channels.add((123, channel.id))

    await service._tick()

    assert channel.messages == [
        "[Reminder] Codeforces Round 1 (Div. 2) starts in 24h",
        "[Reminder] AtCoder Beginner Contest 999 starts in 24h",
    ]
    assert repo.mark_calls == 2
