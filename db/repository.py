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
