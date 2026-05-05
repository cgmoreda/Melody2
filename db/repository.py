from __future__ import annotations

import abc
from collections import defaultdict
import hashlib
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Optional, Protocol

import asyncpg

logger = logging.getLogger(__name__)


def _voice_session_lock_key(guild_id: int, discord_id: int) -> int:
    raw = f"{guild_id}:{discord_id}".encode("ascii")
    digest = hashlib.blake2b(raw, digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def _merge_voice_intervals(rows: Iterable[Any]) -> dict[int, float]:
    intervals_by_user: dict[int, list[tuple[datetime, datetime]]] = defaultdict(list)
    for row in rows:
        try:
            discord_id = int(row["discord_id"])
            start = row["start_ts"]
            end = row["end_ts"]
        except (KeyError, TypeError):
            discord_id = int(row.discord_id)
            start = row.start_ts
            end = row.end_ts
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            continue
        if end <= start:
            continue
        intervals_by_user[discord_id].append((start, end))

    totals: dict[int, float] = {}
    for discord_id, intervals in intervals_by_user.items():
        intervals.sort(key=lambda interval: interval[0])
        current_start, current_end = intervals[0]
        total_seconds = 0.0

        for start, end in intervals[1:]:
            if start <= current_end:
                if end > current_end:
                    current_end = end
                continue

            total_seconds += (current_end - current_start).total_seconds()
            current_start, current_end = start, end

        total_seconds += (current_end - current_start).total_seconds()
        if total_seconds > 0:
            totals[discord_id] = total_seconds

    return totals


@dataclass(slots=True)
class VerifiedUser:
    """Row representation for the verified_users table."""

    discord_id: int
    cf_handle: str
    rating: int
    guild_id: int


@dataclass(slots=True)
class CoachConfig:
    """Row representation for the coach_config table."""

    guild_id: int
    coach_id: int
    waiting_room_id: int
    coach_channel_id: int


@dataclass(slots=True)
class GuildCommandConfig:
    """Per-guild command behavior configuration."""

    guild_id: int
    reminder_preview_limit: int
    roundchanges_max_lines: int
    voicehours_max_lines: int
    voice_check_interval_seconds: int
    voice_confirm_timeout_seconds: int


@dataclass(slots=True)
class GymContest:
    guild_id: int
    contest_id: int
    gym_type: str
    created_by: int
    created_at: datetime


@dataclass(slots=True)
class GymProblemTag:
    guild_id: int
    contest_id: int
    problem_index: str
    tag: str
    created_by: int
    created_at: datetime


@dataclass(slots=True)
class GymProblemRatingVote:
    guild_id: int
    contest_id: int
    problem_index: str
    discord_id: int
    estimated_rating: int
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class GymQualityVote:
    guild_id: int
    contest_id: int
    discord_id: int
    quality: int
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class GymParticipationCache:
    guild_id: int
    contest_id: int
    discord_id: int
    solved_count: int
    checked_at: datetime


@dataclass(slots=True)
class PendingVerification:
    guild_id: int
    discord_id: int
    cf_handle: str
    verification_code: str
    created_at: datetime
    expires_at: datetime


@dataclass(slots=True)
class TrackedVoiceInterval:
    discord_id: int
    start_ts: datetime
    end_ts: datetime


@dataclass(slots=True)
class DailySheetReminderConfig:
    guild_id: int
    channel_id: int
    remind_hour_utc: int
    remind_minute_utc: int
    message: str
    last_sent_on: Optional[date]


class VerificationReadRepository(Protocol):
    async def get_by_discord_id(self, discord_id: int, guild_id: int) -> Optional[VerifiedUser]:
        ...

    async def get_all(self, guild_id: int) -> list[VerifiedUser]:
        ...


class VerificationRepository(VerificationReadRepository, Protocol):
    async def upsert(self, user: VerifiedUser) -> None:
        ...

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
        ...

    async def get_pending_verification(self, guild_id: int, discord_id: int) -> Optional[PendingVerification]:
        ...

    async def delete_pending_verification(self, guild_id: int, discord_id: int) -> bool:
        ...


class ReminderRepository(Protocol):
    async def add_reminder_channel(self, guild_id: int, channel_id: int) -> bool:
        ...

    async def remove_reminder_channel(self, guild_id: int, channel_id: int) -> bool:
        ...

    async def get_reminder_channels(self) -> list[tuple[int, int]]:
        ...

    async def has_sent_contest_reminder(
        self,
        guild_id: int,
        channel_id: int,
        contest_id: str,
        reminder_type: str,
        platform: str = "codeforces",
    ) -> bool:
        ...

    async def mark_contest_reminder_sent(
        self,
        guild_id: int,
        channel_id: int,
        contest_id: str,
        reminder_type: str,
        sent_at: datetime,
        platform: str = "codeforces",
    ) -> bool:
        ...

    async def cleanup_old_sent_contest_reminders(self, before: datetime) -> int:
        ...


class DailySheetReminderRepository(Protocol):
    async def upsert_daily_sheet_reminder(
        self,
        *,
        guild_id: int,
        channel_id: int,
        remind_hour_utc: int,
        remind_minute_utc: int,
        message: str,
    ) -> None:
        ...

    async def delete_daily_sheet_reminder(self, guild_id: int) -> bool:
        ...

    async def get_daily_sheet_reminder(self, guild_id: int) -> Optional[DailySheetReminderConfig]:
        ...

    async def list_daily_sheet_reminders(self) -> list[DailySheetReminderConfig]:
        ...

    async def mark_daily_sheet_reminder_sent(self, guild_id: int, sent_on: date) -> bool:
        ...


class CoachRepository(Protocol):
    async def get_coach_config(self, guild_id: int) -> Optional[CoachConfig]:
        ...

    async def upsert_coach_config(self, config: CoachConfig) -> None:
        ...

    async def delete_coach_config(self, guild_id: int) -> bool:
        ...


class VoiceRepository(Protocol):
    async def start_voice_session(
        self,
        guild_id: int,
        discord_id: int,
        channel_id: int,
        channel_name: str,
        is_tracked: bool,
        started_at: datetime,
    ) -> None:
        ...

    async def close_open_voice_sessions(self, guild_id: int, discord_id: int, ended_at: datetime) -> int:
        ...

    async def has_open_voice_session(self, guild_id: int, discord_id: int) -> bool:
        ...

    async def get_open_tracked_voice_member_ids(self, guild_id: int) -> set[int]:
        ...

    async def get_tracked_voice_totals(
        self,
        guild_id: int,
        *,
        now: datetime,
        since: Optional[datetime] = None,
    ) -> dict[int, float]:
        ...

    async def get_tracked_voice_intervals(
        self,
        guild_id: int,
        *,
        since: datetime,
        now: datetime,
    ) -> list[TrackedVoiceInterval]:
        ...

    async def get_tracked_voice_summary(
        self,
        guild_id: int,
        *,
        now: datetime,
        week_since: datetime,
        month_since: datetime,
    ) -> dict[int, dict[str, float]]:
        ...


class GuildConfigRepository(Protocol):
    async def get_guild_command_config(self, guild_id: int) -> Optional[GuildCommandConfig]:
        ...

    async def upsert_guild_command_config(self, config: GuildCommandConfig) -> None:
        ...

    async def delete_guild_command_config(self, guild_id: int) -> bool:
        ...

    async def get_guild_text_config(self, guild_id: int, key: str) -> Optional[str]:
        ...

    async def list_guild_text_configs(self, guild_id: int) -> dict[str, str]:
        ...

    async def upsert_guild_text_config(self, guild_id: int, key: str, value: str) -> None:
        ...

    async def delete_guild_text_config(self, guild_id: int, key: str) -> bool:
        ...


class GymRepository(Protocol):
    async def upsert_gym_contest(self, guild_id: int, contest_id: int, gym_type: str, created_by: int) -> None:
        ...

    async def get_gym_contest(self, guild_id: int, contest_id: int) -> Optional[GymContest]:
        ...

    async def list_gym_contests(self, guild_id: int) -> list[GymContest]:
        ...

    async def add_gym_problem_tag(
        self,
        guild_id: int,
        contest_id: int,
        problem_index: str,
        tag: str,
        created_by: int,
    ) -> bool:
        ...

    async def remove_gym_problem_tag(
        self,
        guild_id: int,
        contest_id: int,
        problem_index: str,
        tag: str,
    ) -> bool:
        ...

    async def list_gym_problem_tags(self, guild_id: int, contest_id: int) -> list[GymProblemTag]:
        ...

    async def upsert_gym_problem_rating_vote(
        self,
        guild_id: int,
        contest_id: int,
        problem_index: str,
        discord_id: int,
        estimated_rating: int,
    ) -> None:
        ...

    async def list_gym_problem_rating_votes(
        self,
        guild_id: int,
        contest_id: int,
        problem_index: str,
    ) -> list[GymProblemRatingVote]:
        ...

    async def upsert_gym_quality_vote(
        self,
        guild_id: int,
        contest_id: int,
        discord_id: int,
        quality: int,
    ) -> None:
        ...

    async def list_gym_quality_votes(self, guild_id: int, contest_id: int) -> list[GymQualityVote]:
        ...

    async def delete_gym_contest(self, guild_id: int, contest_id: int) -> bool:
        ...

    async def reset_gym_contest_data(self, guild_id: int, contest_id: int) -> None:
        ...

    async def get_gym_participation_cache(self, guild_id: int, contest_id: int) -> list[GymParticipationCache]:
        ...

    async def upsert_gym_participation_cache(
        self,
        guild_id: int,
        contest_id: int,
        discord_id: int,
        solved_count: int,
        checked_at: datetime,
    ) -> None:
        ...

    async def clear_gym_participation_cache(self, guild_id: int, contest_id: int) -> None:
        ...


class VoiceFeatureRepository(VoiceRepository, VerificationReadRepository, Protocol):
    ...


class GymFeatureRepository(GymRepository, VerificationReadRepository, Protocol):
    ...


class UserRepositoryBase(abc.ABC):
    """Abstraction over persistent user storage."""

    @abc.abstractmethod
    async def init(self) -> None:
        """Create tables if they don't exist yet."""

    @abc.abstractmethod
    async def upsert(self, user: VerifiedUser) -> None:
        """Insert or update a verified user record."""

    @abc.abstractmethod
    async def get_by_discord_id(self, discord_id: int, guild_id: int) -> Optional[VerifiedUser]:
        """Look up a verified user by their Discord snowflake."""

    @abc.abstractmethod
    async def get_all(self, guild_id: int) -> list[VerifiedUser]:
        """Return every verified user in a guild."""

    @abc.abstractmethod
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
        """Create or replace a pending verification row for one guild/user pair."""

    @abc.abstractmethod
    async def get_pending_verification(self, guild_id: int, discord_id: int) -> Optional[PendingVerification]:
        """Return one pending verification row, if present."""

    @abc.abstractmethod
    async def delete_pending_verification(self, guild_id: int, discord_id: int) -> bool:
        """Delete one pending verification row. Returns True when a row existed."""

    @abc.abstractmethod
    async def add_reminder_channel(self, guild_id: int, channel_id: int) -> bool:
        """Enable contest reminders for a guild channel."""

    @abc.abstractmethod
    async def remove_reminder_channel(self, guild_id: int, channel_id: int) -> bool:
        """Disable contest reminders for a guild channel."""

    @abc.abstractmethod
    async def get_reminder_channels(self) -> list[tuple[int, int]]:
        """Return all enabled reminder channels as (guild_id, channel_id)."""

    @abc.abstractmethod
    async def has_sent_contest_reminder(
        self,
        guild_id: int,
        channel_id: int,
        contest_id: str,
        reminder_type: str,
        platform: str = "codeforces",
    ) -> bool:
        """Return whether this reminder dispatch key has already been recorded."""

    @abc.abstractmethod
    async def mark_contest_reminder_sent(
        self,
        guild_id: int,
        channel_id: int,
        contest_id: str,
        reminder_type: str,
        sent_at: datetime,
        platform: str = "codeforces",
    ) -> bool:
        """Record one sent reminder. Returns True when inserted, False when it already existed."""

    @abc.abstractmethod
    async def cleanup_old_sent_contest_reminders(self, before: datetime) -> int:
        """Delete old persisted reminder dispatch rows and return deleted row count."""

    @abc.abstractmethod
    async def upsert_daily_sheet_reminder(
        self,
        *,
        guild_id: int,
        channel_id: int,
        remind_hour_utc: int,
        remind_minute_utc: int,
        message: str,
    ) -> None:
        """Create or replace the daily sheets reminder config for one guild."""

    @abc.abstractmethod
    async def delete_daily_sheet_reminder(self, guild_id: int) -> bool:
        """Delete the daily sheets reminder config for one guild."""

    @abc.abstractmethod
    async def get_daily_sheet_reminder(self, guild_id: int) -> Optional[DailySheetReminderConfig]:
        """Return the daily sheets reminder config for one guild, if set."""

    @abc.abstractmethod
    async def list_daily_sheet_reminders(self) -> list[DailySheetReminderConfig]:
        """Return every configured daily sheets reminder."""

    @abc.abstractmethod
    async def mark_daily_sheet_reminder_sent(self, guild_id: int, sent_on: date) -> bool:
        """Mark one guild's daily sheets reminder as sent for the given date."""

    @abc.abstractmethod
    async def get_coach_config(self, guild_id: int) -> Optional[CoachConfig]:
        """Return coach config for a guild, or None."""

    @abc.abstractmethod
    async def upsert_coach_config(self, config: CoachConfig) -> None:
        """Insert or update coach config for a guild."""

    @abc.abstractmethod
    async def delete_coach_config(self, guild_id: int) -> bool:
        """Remove coach config for a guild. Returns True if a row was deleted."""

    @abc.abstractmethod
    async def start_voice_session(
        self,
        guild_id: int,
        discord_id: int,
        channel_id: int,
        channel_name: str,
        is_tracked: bool,
        started_at: datetime,
    ) -> None:
        """Insert a new voice session entry."""

    @abc.abstractmethod
    async def close_open_voice_sessions(self, guild_id: int, discord_id: int, ended_at: datetime) -> int:
        """Close all open voice sessions for a user in a guild."""

    @abc.abstractmethod
    async def has_open_voice_session(self, guild_id: int, discord_id: int) -> bool:
        """Return whether the user currently has an open voice session row."""

    @abc.abstractmethod
    async def get_open_tracked_voice_member_ids(self, guild_id: int) -> set[int]:
        """Return member IDs that currently have open tracked voice sessions in a guild."""

    @abc.abstractmethod
    async def get_tracked_voice_totals(
        self,
        guild_id: int,
        *,
        now: datetime,
        since: Optional[datetime] = None,
    ) -> dict[int, float]:
        """Return total tracked-channel voice time in seconds per user."""

    @abc.abstractmethod
    async def get_tracked_voice_intervals(
        self,
        guild_id: int,
        *,
        since: datetime,
        now: datetime,
    ) -> list[TrackedVoiceInterval]:
        """Return clipped tracked voice intervals for one guild and UTC range."""

    @abc.abstractmethod
    async def get_tracked_voice_summary(
        self,
        guild_id: int,
        *,
        now: datetime,
        week_since: datetime,
        month_since: datetime,
    ) -> dict[int, dict[str, float]]:
        """Return per-user tracked totals for week, month, and all time in one query."""

    @abc.abstractmethod
    async def get_guild_command_config(self, guild_id: int) -> Optional[GuildCommandConfig]:
        """Return persisted command config for a guild if one exists."""

    @abc.abstractmethod
    async def upsert_guild_command_config(self, config: GuildCommandConfig) -> None:
        """Insert or update command config for a guild."""

    @abc.abstractmethod
    async def delete_guild_command_config(self, guild_id: int) -> bool:
        """Delete command config for a guild. Returns True if a row was deleted."""

    @abc.abstractmethod
    async def upsert_gym_contest(self, guild_id: int, contest_id: int, gym_type: str, created_by: int) -> None:
        """Create or update a gym contest in a guild."""

    @abc.abstractmethod
    async def get_gym_contest(self, guild_id: int, contest_id: int) -> Optional[GymContest]:
        """Get one gym contest row."""

    @abc.abstractmethod
    async def list_gym_contests(self, guild_id: int) -> list[GymContest]:
        """List all gym contests in a guild."""

    @abc.abstractmethod
    async def add_gym_problem_tag(
        self,
        guild_id: int,
        contest_id: int,
        problem_index: str,
        tag: str,
        created_by: int,
    ) -> bool:
        """Add one tag to a gym problem. Returns True when inserted."""

    @abc.abstractmethod
    async def remove_gym_problem_tag(
        self,
        guild_id: int,
        contest_id: int,
        problem_index: str,
        tag: str,
    ) -> bool:
        """Remove one tag from a gym problem. Returns True when removed."""

    @abc.abstractmethod
    async def list_gym_problem_tags(self, guild_id: int, contest_id: int) -> list[GymProblemTag]:
        """List tags for all problems in a gym contest."""

    @abc.abstractmethod
    async def upsert_gym_problem_rating_vote(
        self,
        guild_id: int,
        contest_id: int,
        problem_index: str,
        discord_id: int,
        estimated_rating: int,
    ) -> None:
        """Create or update one user's estimated rating for a gym problem."""

    @abc.abstractmethod
    async def list_gym_problem_rating_votes(
        self,
        guild_id: int,
        contest_id: int,
        problem_index: str,
    ) -> list[GymProblemRatingVote]:
        """List all rating votes for one gym problem."""

    @abc.abstractmethod
    async def upsert_gym_quality_vote(
        self,
        guild_id: int,
        contest_id: int,
        discord_id: int,
        quality: int,
    ) -> None:
        """Create or update one user's quality vote for a gym contest."""

    @abc.abstractmethod
    async def list_gym_quality_votes(self, guild_id: int, contest_id: int) -> list[GymQualityVote]:
        """List all quality votes for one gym contest."""

    @abc.abstractmethod
    async def delete_gym_contest(self, guild_id: int, contest_id: int) -> bool:
        """Delete a gym contest and all related data."""

    @abc.abstractmethod
    async def reset_gym_contest_data(self, guild_id: int, contest_id: int) -> None:
        """Reset tags, ratings, and participation cache for one gym contest."""

    @abc.abstractmethod
    async def get_gym_participation_cache(self, guild_id: int, contest_id: int) -> list[GymParticipationCache]:
        """Return cached participation rows for one gym contest."""

    @abc.abstractmethod
    async def upsert_gym_participation_cache(
        self,
        guild_id: int,
        contest_id: int,
        discord_id: int,
        solved_count: int,
        checked_at: datetime,
    ) -> None:
        """Create or update one participation cache row."""

    @abc.abstractmethod
    async def clear_gym_participation_cache(self, guild_id: int, contest_id: int) -> None:
        """Clear participation cache for one gym contest."""

    @abc.abstractmethod
    async def get_guild_text_config(self, guild_id: int, key: str) -> Optional[str]:
        """Return one string setting value for a guild."""

    @abc.abstractmethod
    async def list_guild_text_configs(self, guild_id: int) -> dict[str, str]:
        """List all string settings for a guild."""

    @abc.abstractmethod
    async def upsert_guild_text_config(self, guild_id: int, key: str, value: str) -> None:
        """Create or update one string setting."""

    @abc.abstractmethod
    async def delete_guild_text_config(self, guild_id: int, key: str) -> bool:
        """Delete one string setting. Returns True if it existed."""

class UserRepository(UserRepositoryBase):
    """Concrete Postgres implementation using asyncpg."""

    _SCHEMA_LOCK_KEY = 3_310_920_014_991
    _LATEST_SCHEMA_VERSION = 7

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._pool: Optional[asyncpg.Pool] = None

    async def init(self) -> None:
        self._pool = await asyncpg.create_pool(self._database_url)
        async with self._pool.acquire() as conn:
            await conn.execute("SELECT pg_advisory_lock($1)", self._SCHEMA_LOCK_KEY)
            try:
                async with conn.transaction():
                    await self._ensure_schema_version_table(conn)
                    current_version = await self._get_schema_version(conn)
                    if current_version is None:
                        current_version = 0
                        await self._set_schema_version(conn, current_version)
                    await self._apply_migrations(conn, current_version)
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", self._SCHEMA_LOCK_KEY)
        logger.info("Database initialised")

    async def _ensure_schema_version_table(self, conn: asyncpg.Connection) -> None:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                id SMALLINT PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL
            )
            """
        )

    async def _get_schema_version(self, conn: asyncpg.Connection) -> Optional[int]:
        value = await conn.fetchval(
            """
            SELECT version
            FROM schema_version
            WHERE id = 1
            """
        )
        if value is None:
            return None
        return int(value)

    async def _set_schema_version(self, conn: asyncpg.Connection, version: int) -> None:
        await conn.execute(
            """
            INSERT INTO schema_version (id, version)
            VALUES (1, $1)
            ON CONFLICT (id)
            DO UPDATE SET version = EXCLUDED.version
            """,
            version,
        )

    async def _apply_migrations(self, conn: asyncpg.Connection, current_version: int) -> None:
        migrations = {
            1: self._migration_001_create_initial_schema,
            2: self._migration_002_add_indexes,
            3: self._migration_003_enforce_integrity_constraints,
            4: self._migration_004_add_platform_to_sent_reminders,
            5: self._migration_005_rename_is_solo_to_is_tracked,
            6: self._migration_006_enforce_single_open_voice_session,
            7: self._migration_007_add_daily_sheet_reminders,
        }

        if current_version > self._LATEST_SCHEMA_VERSION:
            logger.warning(
                "Database schema version %d is newer than app-supported version %d.",
                current_version,
                self._LATEST_SCHEMA_VERSION,
            )
            return

        for version in sorted(migrations):
            if version <= current_version:
                continue
            logger.info("Applying database migration v%d", version)
            await migrations[version](conn)
            await self._set_schema_version(conn, version)

    async def _migration_001_create_initial_schema(self, conn: asyncpg.Connection) -> None:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS verified_users (
                discord_id BIGINT NOT NULL,
                guild_id BIGINT NOT NULL,
                cf_handle TEXT NOT NULL,
                rating INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (discord_id, guild_id)
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_verifications (
                guild_id BIGINT NOT NULL,
                discord_id BIGINT NOT NULL,
                cf_handle TEXT NOT NULL,
                verification_code TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (guild_id, discord_id)
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reminder_channels (
                guild_id BIGINT NOT NULL,
                channel_id BIGINT NOT NULL,
                PRIMARY KEY (guild_id, channel_id)
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sent_reminders (
                guild_id BIGINT NOT NULL,
                channel_id BIGINT NOT NULL,
                contest_id BIGINT NOT NULL,
                reminder_type TEXT NOT NULL,
                sent_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (guild_id, channel_id, contest_id, reminder_type)
            )
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_sent_reminders_sent_at
            ON sent_reminders (sent_at)
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS coach_config (
                guild_id         BIGINT NOT NULL PRIMARY KEY,
                coach_id         BIGINT NOT NULL,
                waiting_room_id  BIGINT NOT NULL,
                coach_channel_id BIGINT NOT NULL
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS voice_sessions (
                id BIGSERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                discord_id BIGINT NOT NULL,
                channel_id BIGINT NOT NULL,
                channel_name TEXT NOT NULL,
                is_tracked BOOLEAN NOT NULL DEFAULT FALSE,
                started_at TIMESTAMPTZ NOT NULL,
                ended_at TIMESTAMPTZ
            )
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_voice_sessions_guild_user
            ON voice_sessions (guild_id, discord_id, started_at)
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_command_config (
                guild_id BIGINT PRIMARY KEY,
                reminder_preview_limit INTEGER NOT NULL DEFAULT 3 CHECK (reminder_preview_limit BETWEEN 1 AND 10),
                roundchanges_max_lines INTEGER NOT NULL DEFAULT 30 CHECK (roundchanges_max_lines BETWEEN 5 AND 60),
                voicehours_max_lines INTEGER NOT NULL DEFAULT 35 CHECK (voicehours_max_lines BETWEEN 5 AND 100),
                voice_check_interval_seconds INTEGER NOT NULL DEFAULT 900 CHECK (voice_check_interval_seconds BETWEEN 60 AND 7200),
                voice_confirm_timeout_seconds INTEGER NOT NULL DEFAULT 180 CHECK (voice_confirm_timeout_seconds BETWEEN 60 AND 900)
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gym_contests (
                guild_id BIGINT NOT NULL,
                contest_id BIGINT NOT NULL,
                gym_type TEXT NOT NULL CHECK (gym_type IN ('individual', 'team')),
                created_by BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (guild_id, contest_id)
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gym_problem_tags (
                guild_id BIGINT NOT NULL,
                contest_id BIGINT NOT NULL,
                problem_index TEXT NOT NULL,
                tag TEXT NOT NULL,
                created_by BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (guild_id, contest_id, problem_index, tag)
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gym_problem_ratings (
                guild_id BIGINT NOT NULL,
                contest_id BIGINT NOT NULL,
                problem_index TEXT NOT NULL,
                discord_id BIGINT NOT NULL,
                estimated_rating INTEGER NOT NULL CHECK (estimated_rating BETWEEN 300 AND 5000),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (guild_id, contest_id, problem_index, discord_id)
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gym_quality_ratings (
                guild_id BIGINT NOT NULL,
                contest_id BIGINT NOT NULL,
                discord_id BIGINT NOT NULL,
                quality INTEGER NOT NULL CHECK (quality BETWEEN 1 AND 5),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (guild_id, contest_id, discord_id)
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gym_participation_cache (
                guild_id BIGINT NOT NULL,
                contest_id BIGINT NOT NULL,
                discord_id BIGINT NOT NULL,
                solved_count INTEGER NOT NULL DEFAULT 0,
                checked_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (guild_id, contest_id, discord_id)
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_text_config (
                guild_id BIGINT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (guild_id, key)
            )
            """
        )

    async def _migration_002_add_indexes(self, conn: asyncpg.Connection) -> None:
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_verified_users_guild_id
            ON verified_users (guild_id)
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pending_verifications_expires_at
            ON pending_verifications (expires_at)
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_voice_sessions_open_guild_user
            ON voice_sessions (guild_id, discord_id)
            WHERE ended_at IS NULL
            """
        )
        if await self._column_exists(conn, "voice_sessions", "is_tracked"):
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_voice_sessions_tracked_started
                ON voice_sessions (guild_id, is_tracked, started_at)
                """
            )
        elif await self._column_exists(conn, "voice_sessions", "is_solo"):
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_voice_sessions_solo_started
                ON voice_sessions (guild_id, is_solo, started_at)
                """
            )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_gym_problem_tags_problem
            ON gym_problem_tags (guild_id, contest_id, problem_index)
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_gym_quality_ratings_updated
            ON gym_quality_ratings (guild_id, contest_id, updated_at DESC)
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_gym_participation_cache_checked
            ON gym_participation_cache (guild_id, contest_id, checked_at DESC)
            """
        )

    async def _migration_003_enforce_integrity_constraints(self, conn: asyncpg.Connection) -> None:
        await conn.execute(
            """
            WITH ranked AS (
                SELECT
                    ctid,
                    ROW_NUMBER() OVER (
                        PARTITION BY guild_id, LOWER(cf_handle)
                        ORDER BY rating DESC, discord_id ASC
                    ) AS rn
                FROM verified_users
            )
            DELETE FROM verified_users v
            USING ranked r
            WHERE v.ctid = r.ctid
              AND r.rn > 1
            """
        )
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_verified_users_guild_cf_handle_ci
            ON verified_users (guild_id, LOWER(cf_handle))
            """
        )

        await self._ensure_gym_fk_constraint(
            conn,
            table_name="gym_problem_tags",
            constraint_name="fk_gym_problem_tags_contest",
        )
        await self._ensure_gym_fk_constraint(
            conn,
            table_name="gym_problem_ratings",
            constraint_name="fk_gym_problem_ratings_contest",
        )
        await self._ensure_gym_fk_constraint(
            conn,
            table_name="gym_quality_ratings",
            constraint_name="fk_gym_quality_ratings_contest",
        )
        await self._ensure_gym_fk_constraint(
            conn,
            table_name="gym_participation_cache",
            constraint_name="fk_gym_participation_cache_contest",
        )

    async def _ensure_gym_fk_constraint(
        self,
        conn: asyncpg.Connection,
        *,
        table_name: str,
        constraint_name: str,
    ) -> None:
        await conn.execute(
            f"""
            DELETE FROM {table_name} child
            WHERE NOT EXISTS (
                SELECT 1
                FROM gym_contests parent
                WHERE parent.guild_id = child.guild_id
                  AND parent.contest_id = child.contest_id
            )
            """
        )
        if await self._constraint_exists(conn, table_name, constraint_name):
            return
        await conn.execute(
            f"""
            ALTER TABLE {table_name}
            ADD CONSTRAINT {constraint_name}
            FOREIGN KEY (guild_id, contest_id)
            REFERENCES gym_contests (guild_id, contest_id)
            ON DELETE CASCADE
            """
        )

    async def _constraint_exists(
        self,
        conn: asyncpg.Connection,
        table_name: str,
        constraint_name: str,
    ) -> bool:
        row = await conn.fetchval(
            """
            SELECT 1
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname = current_schema()
              AND t.relname = $1
              AND c.conname = $2
            LIMIT 1
            """,
            table_name,
            constraint_name,
        )
        return row is not None

    async def _column_exists(
        self,
        conn: asyncpg.Connection,
        table_name: str,
        column_name: str,
    ) -> bool:
        row = await conn.fetchval(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = $1
              AND column_name = $2
            LIMIT 1
            """,
            table_name,
            column_name,
        )
        return row is not None

    async def _migration_004_add_platform_to_sent_reminders(self, conn: asyncpg.Connection) -> None:
        # Add platform column (default existing rows to 'codeforces').
        await conn.execute(
            """
            ALTER TABLE sent_reminders
            ADD COLUMN IF NOT EXISTS platform TEXT NOT NULL DEFAULT 'codeforces'
            """
        )

        # Convert contest_id from BIGINT to TEXT so string-based IDs (AtCoder) work.
        await conn.execute(
            """
            ALTER TABLE sent_reminders
            ALTER COLUMN contest_id TYPE TEXT USING contest_id::TEXT
            """
        )

        # Rebuild primary key to include platform.
        await conn.execute(
            """
            ALTER TABLE sent_reminders
            DROP CONSTRAINT IF EXISTS sent_reminders_pkey
            """
        )
        await conn.execute(
            """
            ALTER TABLE sent_reminders
            ADD PRIMARY KEY (guild_id, channel_id, platform, contest_id, reminder_type)
            """
        )

    async def _migration_005_rename_is_solo_to_is_tracked(self, conn: asyncpg.Connection) -> None:
        # Rename the is_solo column to is_tracked to reflect that keyword-matched
        # (non-solo) channels also set this flag.
        has_solo = await self._column_exists(conn, "voice_sessions", "is_solo")
        has_tracked = await self._column_exists(conn, "voice_sessions", "is_tracked")
        if has_solo and not has_tracked:
            await conn.execute(
                """
                ALTER TABLE voice_sessions
                RENAME COLUMN is_solo TO is_tracked
                """
            )
            has_tracked = True
        elif has_solo and has_tracked:
            logger.warning("voice_sessions has both is_solo and is_tracked; leaving columns unchanged")

        # Drop the old index (created in migration 002) and recreate under the new name.
        await conn.execute("DROP INDEX IF EXISTS idx_voice_sessions_solo_started")
        if has_tracked:
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_voice_sessions_tracked_started
                ON voice_sessions (guild_id, is_tracked, started_at)
                """
            )

    async def _migration_006_enforce_single_open_voice_session(self, conn: asyncpg.Connection) -> None:
        await conn.execute(
            """
            WITH ranked AS (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY guild_id, discord_id
                        ORDER BY started_at DESC, id DESC
                    ) AS rn,
                    FIRST_VALUE(started_at) OVER (
                        PARTITION BY guild_id, discord_id
                        ORDER BY started_at DESC, id DESC
                    ) AS kept_started_at
                FROM voice_sessions
                WHERE ended_at IS NULL
            )
            UPDATE voice_sessions v
            SET ended_at = GREATEST(v.started_at, ranked.kept_started_at)
            FROM ranked
            WHERE v.id = ranked.id
              AND ranked.rn > 1
              AND v.ended_at IS NULL
            """
        )
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_voice_sessions_one_open_per_member
            ON voice_sessions (guild_id, discord_id)
            WHERE ended_at IS NULL
            """
        )

    async def _migration_007_add_daily_sheet_reminders(self, conn: asyncpg.Connection) -> None:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_sheet_reminders (
                guild_id BIGINT PRIMARY KEY,
                channel_id BIGINT NOT NULL,
                remind_hour_utc SMALLINT NOT NULL CHECK (remind_hour_utc BETWEEN 0 AND 23),
                remind_minute_utc SMALLINT NOT NULL CHECK (remind_minute_utc BETWEEN 0 AND 59),
                message TEXT NOT NULL,
                last_sent_on DATE
            )
            """
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    async def upsert(self, user: VerifiedUser) -> None:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO verified_users (discord_id, guild_id, cf_handle, rating)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT(discord_id, guild_id)
                DO UPDATE SET cf_handle = excluded.cf_handle,
                              rating = excluded.rating
                """,
                user.discord_id,
                user.guild_id,
                user.cf_handle,
                user.rating,
            )

    async def get_by_discord_id(self, discord_id: int, guild_id: int) -> Optional[VerifiedUser]:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT discord_id, cf_handle, rating, guild_id
                FROM verified_users
                WHERE discord_id = $1 AND guild_id = $2
                """,
                discord_id,
                guild_id,
            )
        if row is None:
            return None
        return VerifiedUser(
            discord_id=row["discord_id"],
            cf_handle=row["cf_handle"],
            rating=row["rating"],
            guild_id=row["guild_id"],
        )

    async def get_all(self, guild_id: int) -> list[VerifiedUser]:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT discord_id, cf_handle, rating, guild_id
                FROM verified_users
                WHERE guild_id = $1
                """,
                guild_id,
            )
        return [
            VerifiedUser(
                discord_id=row["discord_id"],
                cf_handle=row["cf_handle"],
                rating=row["rating"],
                guild_id=row["guild_id"],
            )
            for row in rows
        ]

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
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO pending_verifications (
                    guild_id, discord_id, cf_handle, verification_code, created_at, expires_at
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (guild_id, discord_id)
                DO UPDATE SET cf_handle = excluded.cf_handle,
                              verification_code = excluded.verification_code,
                              created_at = excluded.created_at,
                              expires_at = excluded.expires_at
                """,
                guild_id,
                discord_id,
                cf_handle,
                verification_code,
                created_at,
                expires_at,
            )

    async def get_pending_verification(self, guild_id: int, discord_id: int) -> Optional[PendingVerification]:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT guild_id, discord_id, cf_handle, verification_code, created_at, expires_at
                FROM pending_verifications
                WHERE guild_id = $1 AND discord_id = $2
                """,
                guild_id,
                discord_id,
            )
        if row is None:
            return None
        return PendingVerification(
            guild_id=row["guild_id"],
            discord_id=row["discord_id"],
            cf_handle=row["cf_handle"],
            verification_code=row["verification_code"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )

    async def delete_pending_verification(self, guild_id: int, discord_id: int) -> bool:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM pending_verifications
                WHERE guild_id = $1 AND discord_id = $2
                """,
                guild_id,
                discord_id,
            )
        return result.endswith("1")

    async def add_reminder_channel(self, guild_id: int, channel_id: int) -> bool:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                INSERT INTO reminder_channels (guild_id, channel_id)
                VALUES ($1, $2)
                ON CONFLICT(guild_id, channel_id) DO NOTHING
                """,
                guild_id,
                channel_id,
            )
        return result.endswith("1")

    async def remove_reminder_channel(self, guild_id: int, channel_id: int) -> bool:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM reminder_channels
                WHERE guild_id = $1 AND channel_id = $2
                """,
                guild_id,
                channel_id,
            )
        return result.endswith("1")

    async def get_reminder_channels(self) -> list[tuple[int, int]]:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT guild_id, channel_id
                FROM reminder_channels
                """
            )
        return [(row["guild_id"], row["channel_id"]) for row in rows]

    async def has_sent_contest_reminder(
        self,
        guild_id: int,
        channel_id: int,
        contest_id: str,
        reminder_type: str,
        platform: str = "codeforces",
    ) -> bool:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT 1
                FROM sent_reminders
                WHERE guild_id = $1
                  AND channel_id = $2
                  AND platform = $3
                  AND contest_id = $4
                  AND reminder_type = $5
                LIMIT 1
                """,
                guild_id,
                channel_id,
                platform,
                contest_id,
                reminder_type,
            )
        return row is not None

    async def mark_contest_reminder_sent(
        self,
        guild_id: int,
        channel_id: int,
        contest_id: str,
        reminder_type: str,
        sent_at: datetime,
        platform: str = "codeforces",
    ) -> bool:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                INSERT INTO sent_reminders (
                    guild_id, channel_id, platform, contest_id, reminder_type, sent_at
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (guild_id, channel_id, platform, contest_id, reminder_type) DO NOTHING
                """,
                guild_id,
                channel_id,
                platform,
                contest_id,
                reminder_type,
                sent_at,
            )
        return result.endswith("1")

    async def cleanup_old_sent_contest_reminders(self, before: datetime) -> int:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM sent_reminders
                WHERE sent_at < $1
                """,
                before,
            )
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0

    async def upsert_daily_sheet_reminder(
        self,
        *,
        guild_id: int,
        channel_id: int,
        remind_hour_utc: int,
        remind_minute_utc: int,
        message: str,
    ) -> None:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO daily_sheet_reminders (
                    guild_id,
                    channel_id,
                    remind_hour_utc,
                    remind_minute_utc,
                    message,
                    last_sent_on
                )
                VALUES ($1, $2, $3, $4, $5, NULL)
                ON CONFLICT (guild_id)
                DO UPDATE SET channel_id = EXCLUDED.channel_id,
                              remind_hour_utc = EXCLUDED.remind_hour_utc,
                              remind_minute_utc = EXCLUDED.remind_minute_utc,
                              message = EXCLUDED.message,
                              last_sent_on = NULL
                """,
                guild_id,
                channel_id,
                remind_hour_utc,
                remind_minute_utc,
                message,
            )

    async def delete_daily_sheet_reminder(self, guild_id: int) -> bool:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM daily_sheet_reminders
                WHERE guild_id = $1
                """,
                guild_id,
            )
        return result.endswith("1")

    async def get_daily_sheet_reminder(self, guild_id: int) -> Optional[DailySheetReminderConfig]:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT guild_id,
                       channel_id,
                       remind_hour_utc,
                       remind_minute_utc,
                       message,
                       last_sent_on
                FROM daily_sheet_reminders
                WHERE guild_id = $1
                """,
                guild_id,
            )
        if row is None:
            return None
        return DailySheetReminderConfig(
            guild_id=row["guild_id"],
            channel_id=row["channel_id"],
            remind_hour_utc=row["remind_hour_utc"],
            remind_minute_utc=row["remind_minute_utc"],
            message=row["message"],
            last_sent_on=row["last_sent_on"],
        )

    async def list_daily_sheet_reminders(self) -> list[DailySheetReminderConfig]:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT guild_id,
                       channel_id,
                       remind_hour_utc,
                       remind_minute_utc,
                       message,
                       last_sent_on
                FROM daily_sheet_reminders
                ORDER BY guild_id
                """
            )
        return [
            DailySheetReminderConfig(
                guild_id=row["guild_id"],
                channel_id=row["channel_id"],
                remind_hour_utc=row["remind_hour_utc"],
                remind_minute_utc=row["remind_minute_utc"],
                message=row["message"],
                last_sent_on=row["last_sent_on"],
            )
            for row in rows
        ]

    async def mark_daily_sheet_reminder_sent(self, guild_id: int, sent_on: date) -> bool:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE daily_sheet_reminders
                SET last_sent_on = $2
                WHERE guild_id = $1
                  AND (last_sent_on IS NULL OR last_sent_on < $2)
                """,
                guild_id,
                sent_on,
            )
        return result.endswith("1")

    async def get_coach_config(self, guild_id: int) -> Optional[CoachConfig]:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT guild_id, coach_id, waiting_room_id, coach_channel_id
                FROM coach_config
                WHERE guild_id = $1
                """,
                guild_id,
            )
        if row is None:
            return None
        return CoachConfig(
            guild_id=row["guild_id"],
            coach_id=row["coach_id"],
            waiting_room_id=row["waiting_room_id"],
            coach_channel_id=row["coach_channel_id"],
        )

    async def upsert_coach_config(self, config: CoachConfig) -> None:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO coach_config (guild_id, coach_id, waiting_room_id, coach_channel_id)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT(guild_id)
                DO UPDATE SET coach_id         = excluded.coach_id,
                              waiting_room_id  = excluded.waiting_room_id,
                              coach_channel_id = excluded.coach_channel_id
                """,
                config.guild_id,
                config.coach_id,
                config.waiting_room_id,
                config.coach_channel_id,
            )

    async def delete_coach_config(self, guild_id: int) -> bool:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM coach_config
                WHERE guild_id = $1
                """,
                guild_id,
            )
        return result.endswith("1")

    async def start_voice_session(
        self,
        guild_id: int,
        discord_id: int,
        channel_id: int,
        channel_name: str,
        is_tracked: bool,
        started_at: datetime,
    ) -> None:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock($1)",
                    _voice_session_lock_key(guild_id, discord_id),
                )
                await conn.execute(
                    """
                    UPDATE voice_sessions
                    SET ended_at = GREATEST(started_at, $3)
                    WHERE guild_id = $1
                      AND discord_id = $2
                      AND ended_at IS NULL
                    """,
                    guild_id,
                    discord_id,
                    started_at,
                )
                await conn.execute(
                    """
                    INSERT INTO voice_sessions (
                        guild_id, discord_id, channel_id, channel_name, is_tracked, started_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    guild_id,
                    discord_id,
                    channel_id,
                    channel_name,
                    is_tracked,
                    started_at,
                )

    async def close_open_voice_sessions(self, guild_id: int, discord_id: int, ended_at: datetime) -> int:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE voice_sessions
                SET ended_at = $3
                WHERE guild_id = $1
                  AND discord_id = $2
                  AND ended_at IS NULL
                """,
                guild_id,
                discord_id,
                ended_at,
            )
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0

    async def has_open_voice_session(self, guild_id: int, discord_id: int) -> bool:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT 1
                FROM voice_sessions
                WHERE guild_id = $1
                  AND discord_id = $2
                  AND ended_at IS NULL
                LIMIT 1
                """,
                guild_id,
                discord_id,
            )
        return row is not None

    async def get_open_tracked_voice_member_ids(self, guild_id: int) -> set[int]:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT discord_id
                FROM voice_sessions
                WHERE guild_id = $1
                  AND is_tracked = TRUE
                  AND ended_at IS NULL
                """,
                guild_id,
            )
        return {int(row["discord_id"]) for row in rows}

    async def get_tracked_voice_totals(
        self,
        guild_id: int,
        *,
        now: datetime,
        since: Optional[datetime] = None,
    ) -> dict[int, float]:
        assert self._pool is not None, "Call init() first"
        if since is not None:
            return _merge_voice_intervals(
                await self.get_tracked_voice_intervals(guild_id, since=since, now=now)
            )

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT discord_id,
                       started_at AS start_ts,
                       LEAST(COALESCE(ended_at, $2), $2) AS end_ts
                FROM voice_sessions
                WHERE guild_id = $1
                  AND is_tracked = TRUE
                  AND started_at < $2
                  AND COALESCE(ended_at, $2) > started_at
                """,
                guild_id,
                now,
            )
        return _merge_voice_intervals(rows)

    async def get_tracked_voice_intervals(
        self,
        guild_id: int,
        *,
        since: datetime,
        now: datetime,
    ) -> list[TrackedVoiceInterval]:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT discord_id,
                       GREATEST(started_at, $2) AS start_ts,
                       LEAST(COALESCE(ended_at, $3), $3) AS end_ts
                FROM voice_sessions
                WHERE guild_id = $1
                  AND is_tracked = TRUE
                  AND started_at < $3
                  AND COALESCE(ended_at, $3) > $2
                ORDER BY discord_id, start_ts
                """,
                guild_id,
                since,
                now,
            )
        return [
            TrackedVoiceInterval(
                discord_id=int(row["discord_id"]),
                start_ts=row["start_ts"],
                end_ts=row["end_ts"],
            )
            for row in rows
        ]

    async def get_tracked_voice_summary(
        self,
        guild_id: int,
        *,
        now: datetime,
        week_since: datetime,
        month_since: datetime,
    ) -> dict[int, dict[str, float]]:
        all_time = await self.get_tracked_voice_totals(guild_id, now=now)
        week = await self.get_tracked_voice_totals(guild_id, now=now, since=week_since)
        month = await self.get_tracked_voice_totals(guild_id, now=now, since=month_since)
        summary: dict[int, dict[str, float]] = {}
        for discord_id in all_time:
            summary[discord_id] = {
                "week": week.get(discord_id, 0.0),
                "month": month.get(discord_id, 0.0),
                "all_time": all_time[discord_id],
            }
        return summary

    async def get_guild_command_config(self, guild_id: int) -> Optional[GuildCommandConfig]:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT guild_id,
                       reminder_preview_limit,
                       roundchanges_max_lines,
                       voicehours_max_lines,
                       voice_check_interval_seconds,
                       voice_confirm_timeout_seconds
                FROM guild_command_config
                WHERE guild_id = $1
                """,
                guild_id,
            )
        if row is None:
            return None
        return GuildCommandConfig(
            guild_id=row["guild_id"],
            reminder_preview_limit=row["reminder_preview_limit"],
            roundchanges_max_lines=row["roundchanges_max_lines"],
            voicehours_max_lines=row["voicehours_max_lines"],
            voice_check_interval_seconds=row["voice_check_interval_seconds"],
            voice_confirm_timeout_seconds=row["voice_confirm_timeout_seconds"],
        )

    async def upsert_guild_command_config(self, config: GuildCommandConfig) -> None:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO guild_command_config (
                    guild_id,
                    reminder_preview_limit,
                    roundchanges_max_lines,
                    voicehours_max_lines,
                    voice_check_interval_seconds,
                    voice_confirm_timeout_seconds
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT(guild_id)
                DO UPDATE SET reminder_preview_limit = excluded.reminder_preview_limit,
                              roundchanges_max_lines = excluded.roundchanges_max_lines,
                              voicehours_max_lines = excluded.voicehours_max_lines,
                              voice_check_interval_seconds = excluded.voice_check_interval_seconds,
                              voice_confirm_timeout_seconds = excluded.voice_confirm_timeout_seconds
                """,
                config.guild_id,
                config.reminder_preview_limit,
                config.roundchanges_max_lines,
                config.voicehours_max_lines,
                config.voice_check_interval_seconds,
                config.voice_confirm_timeout_seconds,
            )

    async def delete_guild_command_config(self, guild_id: int) -> bool:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM guild_command_config
                WHERE guild_id = $1
                """,
                guild_id,
            )
        return result.endswith("1")

    async def upsert_gym_contest(self, guild_id: int, contest_id: int, gym_type: str, created_by: int) -> None:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO gym_contests (guild_id, contest_id, gym_type, created_by)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (guild_id, contest_id)
                DO UPDATE SET gym_type = EXCLUDED.gym_type,
                              created_by = EXCLUDED.created_by
                """,
                guild_id,
                contest_id,
                gym_type,
                created_by,
            )

    async def get_gym_contest(self, guild_id: int, contest_id: int) -> Optional[GymContest]:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT guild_id, contest_id, gym_type, created_by, created_at
                FROM gym_contests
                WHERE guild_id = $1 AND contest_id = $2
                """,
                guild_id,
                contest_id,
            )
        if row is None:
            return None
        return GymContest(
            guild_id=row["guild_id"],
            contest_id=row["contest_id"],
            gym_type=row["gym_type"],
            created_by=row["created_by"],
            created_at=row["created_at"],
        )

    async def list_gym_contests(self, guild_id: int) -> list[GymContest]:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT guild_id, contest_id, gym_type, created_by, created_at
                FROM gym_contests
                WHERE guild_id = $1
                ORDER BY contest_id DESC
                """,
                guild_id,
            )
        return [
            GymContest(
                guild_id=row["guild_id"],
                contest_id=row["contest_id"],
                gym_type=row["gym_type"],
                created_by=row["created_by"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def add_gym_problem_tag(
        self,
        guild_id: int,
        contest_id: int,
        problem_index: str,
        tag: str,
        created_by: int,
    ) -> bool:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                INSERT INTO gym_problem_tags (guild_id, contest_id, problem_index, tag, created_by)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (guild_id, contest_id, problem_index, tag) DO NOTHING
                """,
                guild_id,
                contest_id,
                problem_index,
                tag,
                created_by,
            )
        return result.endswith("1")

    async def remove_gym_problem_tag(
        self,
        guild_id: int,
        contest_id: int,
        problem_index: str,
        tag: str,
    ) -> bool:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM gym_problem_tags
                WHERE guild_id = $1
                  AND contest_id = $2
                  AND problem_index = $3
                  AND tag = $4
                """,
                guild_id,
                contest_id,
                problem_index,
                tag,
            )
        return result.endswith("1")

    async def list_gym_problem_tags(self, guild_id: int, contest_id: int) -> list[GymProblemTag]:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT guild_id, contest_id, problem_index, tag, created_by, created_at
                FROM gym_problem_tags
                WHERE guild_id = $1 AND contest_id = $2
                ORDER BY problem_index, tag
                """,
                guild_id,
                contest_id,
            )
        return [
            GymProblemTag(
                guild_id=row["guild_id"],
                contest_id=row["contest_id"],
                problem_index=row["problem_index"],
                tag=row["tag"],
                created_by=row["created_by"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def upsert_gym_problem_rating_vote(
        self,
        guild_id: int,
        contest_id: int,
        problem_index: str,
        discord_id: int,
        estimated_rating: int,
    ) -> None:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO gym_problem_ratings (
                    guild_id, contest_id, problem_index, discord_id, estimated_rating
                )
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (guild_id, contest_id, problem_index, discord_id)
                DO UPDATE SET estimated_rating = EXCLUDED.estimated_rating,
                              updated_at = NOW()
                """,
                guild_id,
                contest_id,
                problem_index,
                discord_id,
                estimated_rating,
            )

    async def list_gym_problem_rating_votes(
        self,
        guild_id: int,
        contest_id: int,
        problem_index: str,
    ) -> list[GymProblemRatingVote]:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT guild_id, contest_id, problem_index, discord_id, estimated_rating, created_at, updated_at
                FROM gym_problem_ratings
                WHERE guild_id = $1
                  AND contest_id = $2
                  AND problem_index = $3
                ORDER BY updated_at DESC
                """,
                guild_id,
                contest_id,
                problem_index,
            )
        return [
            GymProblemRatingVote(
                guild_id=row["guild_id"],
                contest_id=row["contest_id"],
                problem_index=row["problem_index"],
                discord_id=row["discord_id"],
                estimated_rating=row["estimated_rating"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    async def upsert_gym_quality_vote(
        self,
        guild_id: int,
        contest_id: int,
        discord_id: int,
        quality: int,
    ) -> None:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO gym_quality_ratings (
                    guild_id, contest_id, discord_id, quality
                )
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (guild_id, contest_id, discord_id)
                DO UPDATE SET quality = EXCLUDED.quality,
                              updated_at = NOW()
                """,
                guild_id,
                contest_id,
                discord_id,
                quality,
            )

    async def list_gym_quality_votes(self, guild_id: int, contest_id: int) -> list[GymQualityVote]:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT guild_id, contest_id, discord_id, quality, created_at, updated_at
                FROM gym_quality_ratings
                WHERE guild_id = $1
                  AND contest_id = $2
                ORDER BY updated_at DESC
                """,
                guild_id,
                contest_id,
            )
        return [
            GymQualityVote(
                guild_id=row["guild_id"],
                contest_id=row["contest_id"],
                discord_id=row["discord_id"],
                quality=row["quality"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    async def delete_gym_contest(self, guild_id: int, contest_id: int) -> bool:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    DELETE FROM gym_problem_tags
                    WHERE guild_id = $1 AND contest_id = $2
                    """,
                    guild_id,
                    contest_id,
                )
                await conn.execute(
                    """
                    DELETE FROM gym_problem_ratings
                    WHERE guild_id = $1 AND contest_id = $2
                    """,
                    guild_id,
                    contest_id,
                )
                await conn.execute(
                    """
                    DELETE FROM gym_quality_ratings
                    WHERE guild_id = $1 AND contest_id = $2
                    """,
                    guild_id,
                    contest_id,
                )
                await conn.execute(
                    """
                    DELETE FROM gym_participation_cache
                    WHERE guild_id = $1 AND contest_id = $2
                    """,
                    guild_id,
                    contest_id,
                )
                result = await conn.execute(
                    """
                    DELETE FROM gym_contests
                    WHERE guild_id = $1 AND contest_id = $2
                    """,
                    guild_id,
                    contest_id,
                )
        return result.endswith("1")

    async def reset_gym_contest_data(self, guild_id: int, contest_id: int) -> None:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    DELETE FROM gym_problem_tags
                    WHERE guild_id = $1 AND contest_id = $2
                    """,
                    guild_id,
                    contest_id,
                )
                await conn.execute(
                    """
                    DELETE FROM gym_problem_ratings
                    WHERE guild_id = $1 AND contest_id = $2
                    """,
                    guild_id,
                    contest_id,
                )
                await conn.execute(
                    """
                    DELETE FROM gym_quality_ratings
                    WHERE guild_id = $1 AND contest_id = $2
                    """,
                    guild_id,
                    contest_id,
                )
                await conn.execute(
                    """
                    DELETE FROM gym_participation_cache
                    WHERE guild_id = $1 AND contest_id = $2
                    """,
                    guild_id,
                    contest_id,
                )

    async def get_gym_participation_cache(self, guild_id: int, contest_id: int) -> list[GymParticipationCache]:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT guild_id, contest_id, discord_id, solved_count, checked_at
                FROM gym_participation_cache
                WHERE guild_id = $1 AND contest_id = $2
                """,
                guild_id,
                contest_id,
            )
        return [
            GymParticipationCache(
                guild_id=row["guild_id"],
                contest_id=row["contest_id"],
                discord_id=row["discord_id"],
                solved_count=row["solved_count"],
                checked_at=row["checked_at"],
            )
            for row in rows
        ]

    async def upsert_gym_participation_cache(
        self,
        guild_id: int,
        contest_id: int,
        discord_id: int,
        solved_count: int,
        checked_at: datetime,
    ) -> None:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO gym_participation_cache (
                    guild_id, contest_id, discord_id, solved_count, checked_at
                )
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (guild_id, contest_id, discord_id)
                DO UPDATE SET solved_count = EXCLUDED.solved_count,
                              checked_at = EXCLUDED.checked_at
                """,
                guild_id,
                contest_id,
                discord_id,
                solved_count,
                checked_at,
            )

    async def clear_gym_participation_cache(self, guild_id: int, contest_id: int) -> None:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM gym_participation_cache
                WHERE guild_id = $1 AND contest_id = $2
                """,
                guild_id,
                contest_id,
            )

    async def get_guild_text_config(self, guild_id: int, key: str) -> Optional[str]:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT value
                FROM guild_text_config
                WHERE guild_id = $1 AND key = $2
                """,
                guild_id,
                key,
            )
        if row is None:
            return None
        return row["value"]

    async def list_guild_text_configs(self, guild_id: int) -> dict[str, str]:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT key, value
                FROM guild_text_config
                WHERE guild_id = $1
                """,
                guild_id,
            )
        return {str(row["key"]): str(row["value"]) for row in rows}

    async def upsert_guild_text_config(self, guild_id: int, key: str, value: str) -> None:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO guild_text_config (guild_id, key, value)
                VALUES ($1, $2, $3)
                ON CONFLICT (guild_id, key)
                DO UPDATE SET value = EXCLUDED.value
                """,
                guild_id,
                key,
                value,
            )

    async def delete_guild_text_config(self, guild_id: int, key: str) -> bool:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM guild_text_config
                WHERE guild_id = $1 AND key = $2
                """,
                guild_id,
                key,
            )
        return result.endswith("1")
