from __future__ import annotations

from dataclasses import replace

from db.repository import GuildCommandConfig, UserRepositoryBase


DEFAULT_COMMAND_CONFIG = GuildCommandConfig(
    guild_id=0,
    reminder_preview_limit=3,
    roundchanges_max_lines=30,
    voicehours_max_lines=35,
    voice_check_interval_seconds=900,
    voice_confirm_timeout_seconds=180,
)


CONFIG_SPECS: dict[str, tuple[str, int, int, str]] = {
    "reminder_preview_limit": (
        "reminder_preview_limit",
        1,
        10,
        "How many upcoming contests `!reminder next` shows.",
    ),
    "roundchanges_max_lines": (
        "roundchanges_max_lines",
        5,
        60,
        "Maximum rows shown by `!roundchanges`.",
    ),
    "voicehours_max_lines": (
        "voicehours_max_lines",
        5,
        100,
        "Maximum rows shown by `!voicehours`.",
    ),
    "voice_check_interval_seconds": (
        "voice_check_interval_seconds",
        60,
        7200,
        "Seconds between solo-channel work checks.",
    ),
    "voice_confirm_timeout_seconds": (
        "voice_confirm_timeout_seconds",
        60,
        900,
        "Seconds a user has to confirm they are still working.",
    ),
}


class GuildConfigService:
    def __init__(self, repo: UserRepositoryBase) -> None:
        self._repo = repo
        self._cache: dict[int, GuildCommandConfig] = {}

    async def get(self, guild_id: int) -> GuildCommandConfig:
        cached = self._cache.get(guild_id)
        if cached is not None:
            return cached

        persisted = await self._repo.get_guild_command_config(guild_id)
        if persisted is None:
            config = replace(DEFAULT_COMMAND_CONFIG, guild_id=guild_id)
        else:
            config = persisted

        self._cache[guild_id] = config
        return config

    async def set_value(self, guild_id: int, key: str, value: int) -> GuildCommandConfig:
        if key not in CONFIG_SPECS:
            raise ValueError(f"Unknown config key: {key}")

        attr, minimum, maximum, _ = CONFIG_SPECS[key]
        if not (minimum <= value <= maximum):
            raise ValueError(f"{key} must be between {minimum} and {maximum}")

        current = await self.get(guild_id)
        updated = replace(current)
        setattr(updated, attr, value)

        await self._repo.upsert_guild_command_config(updated)
        self._cache[guild_id] = updated
        return updated

    async def reset_key(self, guild_id: int, key: str) -> GuildCommandConfig:
        if key not in CONFIG_SPECS:
            raise ValueError(f"Unknown config key: {key}")

        attr, _, _, _ = CONFIG_SPECS[key]
        current = await self.get(guild_id)
        updated = replace(current)
        setattr(updated, attr, getattr(DEFAULT_COMMAND_CONFIG, attr))

        await self._repo.upsert_guild_command_config(updated)
        self._cache[guild_id] = updated
        return updated

    async def reset_all(self, guild_id: int) -> GuildCommandConfig:
        await self._repo.delete_guild_command_config(guild_id)
        updated = replace(DEFAULT_COMMAND_CONFIG, guild_id=guild_id)
        self._cache[guild_id] = updated
        return updated