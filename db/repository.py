from __future__ import annotations

import abc
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)


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
    async def add_reminder_channel(self, guild_id: int, channel_id: int) -> bool:
        """Enable contest reminders for a guild channel."""

    @abc.abstractmethod
    async def remove_reminder_channel(self, guild_id: int, channel_id: int) -> bool:
        """Disable contest reminders for a guild channel."""

    @abc.abstractmethod
    async def get_reminder_channels(self) -> list[tuple[int, int]]:
        """Return all enabled reminder channels as (guild_id, channel_id)."""

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
        is_solo: bool,
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
    async def get_solo_voice_totals(
        self,
        guild_id: int,
        *,
        now: datetime,
        since: Optional[datetime] = None,
    ) -> dict[int, float]:
        """Return total solo-channel voice time in seconds per user."""

    @abc.abstractmethod
    async def get_solo_voice_summary(
        self,
        guild_id: int,
        *,
        now: datetime,
        week_since: datetime,
        month_since: datetime,
    ) -> dict[int, dict[str, float]]:
        """Return per-user solo totals for week, month, and all time in one query."""

    @abc.abstractmethod
    async def get_guild_command_config(self, guild_id: int) -> Optional[GuildCommandConfig]:
        """Return persisted command config for a guild if one exists."""

    @abc.abstractmethod
    async def upsert_guild_command_config(self, config: GuildCommandConfig) -> None:
        """Insert or update command config for a guild."""

    @abc.abstractmethod
    async def delete_guild_command_config(self, guild_id: int) -> bool:
        """Delete command config for a guild. Returns True if a row was deleted."""

class UserRepository(UserRepositoryBase):
    """Concrete Postgres implementation using asyncpg."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._pool: Optional[asyncpg.Pool] = None

    async def init(self) -> None:
        self._pool = await asyncpg.create_pool(self._database_url)
        async with self._pool.acquire() as conn:
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
                CREATE TABLE IF NOT EXISTS reminder_channels (
                    guild_id BIGINT NOT NULL,
                    channel_id BIGINT NOT NULL,
                    PRIMARY KEY (guild_id, channel_id)
                )
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
                    is_solo BOOLEAN NOT NULL DEFAULT FALSE,
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
        logger.info("Database initialised")

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
        is_solo: bool,
        started_at: datetime,
    ) -> None:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO voice_sessions (
                    guild_id, discord_id, channel_id, channel_name, is_solo, started_at
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                guild_id,
                discord_id,
                channel_id,
                channel_name,
                is_solo,
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

    async def get_solo_voice_totals(
        self,
        guild_id: int,
        *,
        now: datetime,
        since: Optional[datetime] = None,
    ) -> dict[int, float]:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            if since is None:
                rows = await conn.fetch(
                    """
                    SELECT discord_id,
                           SUM(
                               EXTRACT(
                                   EPOCH FROM (LEAST(COALESCE(ended_at, $2), $2) - started_at)
                               )
                           ) AS seconds
                    FROM voice_sessions
                    WHERE guild_id = $1
                      AND is_solo = TRUE
                      AND started_at < $2
                    GROUP BY discord_id
                    HAVING SUM(
                               EXTRACT(
                                   EPOCH FROM (LEAST(COALESCE(ended_at, $2), $2) - started_at)
                               )
                           ) > 0
                    """,
                    guild_id,
                    now,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT discord_id,
                           SUM(
                               EXTRACT(
                                   EPOCH FROM (
                                       LEAST(COALESCE(ended_at, $3), $3) - GREATEST(started_at, $2)
                                   )
                               )
                           ) AS seconds
                    FROM voice_sessions
                    WHERE guild_id = $1
                      AND is_solo = TRUE
                      AND started_at < $3
                      AND COALESCE(ended_at, $3) > $2
                    GROUP BY discord_id
                    HAVING SUM(
                               EXTRACT(
                                   EPOCH FROM (
                                       LEAST(COALESCE(ended_at, $3), $3) - GREATEST(started_at, $2)
                                   )
                               )
                           ) > 0
                    """,
                    guild_id,
                    since,
                    now,
                )
        return {row["discord_id"]: float(row["seconds"]) for row in rows}

    async def get_solo_voice_summary(
        self,
        guild_id: int,
        *,
        now: datetime,
        week_since: datetime,
        month_since: datetime,
    ) -> dict[int, dict[str, float]]:
        assert self._pool is not None, "Call init() first"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    discord_id,
                    SUM(
                        EXTRACT(
                            EPOCH FROM (LEAST(COALESCE(ended_at, $2), $2) - started_at)
                        )
                    ) AS all_time_seconds,
                    SUM(
                        CASE
                            WHEN COALESCE(ended_at, $2) > $3 THEN
                                EXTRACT(
                                    EPOCH FROM (
                                        LEAST(COALESCE(ended_at, $2), $2) - GREATEST(started_at, $3)
                                    )
                                )
                            ELSE 0
                        END
                    ) AS week_seconds,
                    SUM(
                        CASE
                            WHEN COALESCE(ended_at, $2) > $4 THEN
                                EXTRACT(
                                    EPOCH FROM (
                                        LEAST(COALESCE(ended_at, $2), $2) - GREATEST(started_at, $4)
                                    )
                                )
                            ELSE 0
                        END
                    ) AS month_seconds
                FROM voice_sessions
                WHERE guild_id = $1
                  AND is_solo = TRUE
                  AND started_at < $2
                GROUP BY discord_id
                HAVING SUM(
                           EXTRACT(
                               EPOCH FROM (LEAST(COALESCE(ended_at, $2), $2) - started_at)
                           )
                       ) > 0
                """,
                guild_id,
                now,
                week_since,
                month_since,
            )

        summary: dict[int, dict[str, float]] = {}
        for row in rows:
            summary[row["discord_id"]] = {
                "week": float(row["week_seconds"] or 0.0),
                "month": float(row["month_seconds"] or 0.0),
                "all_time": float(row["all_time_seconds"] or 0.0),
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
