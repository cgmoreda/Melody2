from __future__ import annotations

from dataclasses import replace

from db.repository import GuildCommandConfig, GuildConfigRepository


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

VOICE_CHECK_INTERVAL_KEYS: frozenset[str] = frozenset({
    "voice_check_interval_solo",
    "voice_check_interval_duo",
    "voice_check_interval_team",
    "voice_check_interval_invite",
})

DEFAULT_TEXT_CONFIG: dict[str, str] = {
    "training_role_substring": "training arc",
    "coach_role_substring": "coach",
    "voice_tracked_keywords": "",
    "voice_check_interval_solo": "",
    "voice_check_interval_duo": "",
    "voice_check_interval_team": "",
    "voice_check_interval_invite": "",
}

TEXT_CONFIG_SPECS: dict[str, tuple[int, str]] = {
    "training_role_substring": (
        64,
        "Substring used to detect trainee roles for gym/gald features.",
    ),
    "coach_role_substring": (
        64,
        "Substring used to detect coach roles for gym management.",
    ),
    "voice_tracked_keywords": (
        256,
        "Comma-separated keywords. Channels whose name contains any keyword count in voice hours.",
    ),
    "voice_check_interval_solo": (
        16,
        "Random interval range in minutes for solo channel AFK checks (e.g. 45-60).",
    ),
    "voice_check_interval_duo": (
        16,
        "Random interval range in minutes for duo channel AFK checks (e.g. 60-90).",
    ),
    "voice_check_interval_team": (
        16,
        "Random interval range in minutes for team channel AFK checks (e.g. 75-120).",
    ),
    "voice_check_interval_invite": (
        16,
        "Random interval range in minutes for invite channel AFK checks (e.g. 90-150).",
    ),
}


class GuildConfigService:
    def __init__(self, repo: GuildConfigRepository) -> None:
        self._repo = repo
        self._cache: dict[int, GuildCommandConfig] = {}
        self._text_cache: dict[int, dict[str, str]] = {}

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

    async def get_text(self, guild_id: int, key: str) -> str:
        normalized = key.lower()
        if normalized not in DEFAULT_TEXT_CONFIG:
            raise ValueError(f"Unknown text config key: {key}")
        cached = self._text_cache.get(guild_id)
        if cached is None:
            persisted = await self._repo.list_guild_text_configs(guild_id)
            merged = dict(DEFAULT_TEXT_CONFIG)
            for p_key, p_value in persisted.items():
                if p_key in DEFAULT_TEXT_CONFIG:
                    merged[p_key] = p_value
            self._text_cache[guild_id] = merged
            cached = merged
        return cached[normalized]

    async def get_text_all(self, guild_id: int) -> dict[str, str]:
        await self.get_text(guild_id, "training_role_substring")
        cached = self._text_cache[guild_id]
        return dict(cached)

    async def get_voice_check_interval(
        self, guild_id: int, channel_type: str,
    ) -> tuple[int, int]:
        """Return ``(min_seconds, max_seconds)`` for the voice check interval.

        If a per-channel-type range is configured (e.g. ``"45-60"`` minutes),
        it is parsed and converted to seconds.  Otherwise the legacy
        ``voice_check_interval_seconds`` integer value is used for both
        bounds, providing transparent backwards compatibility.
        """
        key = f"voice_check_interval_{channel_type}"
        raw = await self.get_text(guild_id, key)
        if raw:
            return self.parse_interval_range(raw)
        config = await self.get(guild_id)
        s = config.voice_check_interval_seconds
        return (s, s)

    @staticmethod
    def parse_interval_range(raw: str) -> tuple[int, int]:
        """Parse ``'MIN-MAX'`` minutes into ``(min_seconds, max_seconds)``."""
        parts = raw.strip().split("-")
        if len(parts) != 2:
            raise ValueError(
                f"Invalid interval range: {raw!r}. Expected format: MIN-MAX"
            )
        try:
            min_minutes = int(parts[0].strip())
            max_minutes = int(parts[1].strip())
        except ValueError:
            raise ValueError(
                f"Invalid interval range: {raw!r}. MIN and MAX must be integers."
            )
        if min_minutes <= 0 or max_minutes <= 0:
            raise ValueError("Interval values must be positive.")
        if min_minutes > max_minutes:
            raise ValueError(
                f"MIN ({min_minutes}) must be <= MAX ({max_minutes})."
            )
        return (min_minutes * 60, max_minutes * 60)

    async def set_text(self, guild_id: int, key: str, value: str) -> str:
        normalized = key.lower()
        if normalized not in TEXT_CONFIG_SPECS:
            raise ValueError(f"Unknown text config key: {key}")
        clean = value.strip()
        max_len, _ = TEXT_CONFIG_SPECS[normalized]
        if not clean:
            raise ValueError(f"{normalized} cannot be empty")
        if len(clean) > max_len:
            raise ValueError(f"{normalized} is too long (max {max_len} chars)")

        if normalized in VOICE_CHECK_INTERVAL_KEYS:
            self.parse_interval_range(clean)

        await self._repo.upsert_guild_text_config(guild_id, normalized, clean)
        cached = await self.get_text_all(guild_id)
        cached[normalized] = clean
        self._text_cache[guild_id] = cached
        return clean

    async def reset_text(self, guild_id: int, key: str) -> str:
        normalized = key.lower()
        if normalized not in DEFAULT_TEXT_CONFIG:
            raise ValueError(f"Unknown text config key: {key}")
        await self._repo.delete_guild_text_config(guild_id, normalized)
        cached = await self.get_text_all(guild_id)
        cached[normalized] = DEFAULT_TEXT_CONFIG[normalized]
        self._text_cache[guild_id] = cached
        return cached[normalized]

    async def reset_text_all(self, guild_id: int) -> dict[str, str]:
        for key in DEFAULT_TEXT_CONFIG:
            await self._repo.delete_guild_text_config(guild_id, key)
        self._text_cache[guild_id] = dict(DEFAULT_TEXT_CONFIG)
        return dict(self._text_cache[guild_id])
