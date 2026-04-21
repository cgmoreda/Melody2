# db/repository.py
# SQLite-backed storage for verified CF ↔ Discord mappings.
# Usage: repo = UserRepository("data/verified.db"); await repo.init()

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class VerifiedUser:
    """Row representation for the ``verified_users`` table."""

    discord_id: int
    cf_handle: str
    rating: int
    guild_id: int


class UserRepositoryBase(abc.ABC):
    """Abstraction over persistent user storage (DIP)."""

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


class UserRepository(UserRepositoryBase):
    """Concrete SQLite implementation using *aiosqlite*."""

    def __init__(self, db_path: str = "data/verified.db") -> None:
        self._db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    # ── lifecycle ──────────────────────────────────────────────

    async def init(self) -> None:
        import os
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)

        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS verified_users (
                discord_id  INTEGER NOT NULL,
                guild_id    INTEGER NOT NULL,
                cf_handle   TEXT    NOT NULL,
                rating      INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (discord_id, guild_id)
            )
            """
        )
        await self._db.commit()
        logger.info("Database initialised at %s", self._db_path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    # ── CRUD ───────────────────────────────────────────────────

    async def upsert(self, user: VerifiedUser) -> None:
        assert self._db is not None, "Call init() first"
        await self._db.execute(
            """
            INSERT INTO verified_users (discord_id, guild_id, cf_handle, rating)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(discord_id, guild_id)
            DO UPDATE SET cf_handle = excluded.cf_handle,
                          rating    = excluded.rating
            """,
            (user.discord_id, user.guild_id, user.cf_handle, user.rating),
        )
        await self._db.commit()

    async def get_by_discord_id(self, discord_id: int, guild_id: int) -> Optional[VerifiedUser]:
        assert self._db is not None, "Call init() first"
        async with self._db.execute(
            "SELECT discord_id, cf_handle, rating, guild_id FROM verified_users WHERE discord_id = ? AND guild_id = ?",
            (discord_id, guild_id),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return VerifiedUser(discord_id=row[0], cf_handle=row[1], rating=row[2], guild_id=row[3])

    async def get_all(self, guild_id: int) -> list[VerifiedUser]:
        assert self._db is not None, "Call init() first"
        async with self._db.execute(
            "SELECT discord_id, cf_handle, rating, guild_id FROM verified_users WHERE guild_id = ?",
            (guild_id,),
        ) as cursor:
            return [
                VerifiedUser(discord_id=r[0], cf_handle=r[1], rating=r[2], guild_id=r[3])
                async for r in cursor
            ]
