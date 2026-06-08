"""Voice alert service for playing audio in Discord voice channels."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import discord

logger = logging.getLogger(__name__)

_DEFAULT_AUDIO_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets",
    "voices",
    "afk-check.mp3",
)

# Maximum time to wait for the entire play_alert operation.
_PLAY_TIMEOUT_SECONDS = 15.0


class VoiceAlertService:
    """Manages voice channel connections for short audio playback.

    Provides guild-level locking so only one voice alert can be active
    per guild at a time, and guarantees cleanup of the voice client on
    every exit path.
    """

    def __init__(self, audio_path: str | None = None) -> None:
        self._audio_path = audio_path or _DEFAULT_AUDIO_PATH
        self._guild_locks: dict[int, asyncio.Lock] = {}

    def _guild_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._guild_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._guild_locks[guild_id] = lock
        return lock

    async def play_alert(
        self,
        channel: discord.VoiceChannel,
        *,
        audio_path: str | None = None,
    ) -> bool:
        """Join *channel*, play an audio file, then disconnect.

        Returns ``True`` when the audio was played successfully,
        ``False`` on any failure (e.g., bot already connected elsewhere,
        missing permissions, FFmpeg error, or timeout).
        """
        path = audio_path or self._audio_path
        if not os.path.isfile(path):
            logger.error("Audio file not found: %s", path)
            return False

        guild = channel.guild
        lock = self._guild_lock(guild.id)

        try:
            return await asyncio.wait_for(
                self._play_locked(lock, channel, path),
                timeout=_PLAY_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Voice alert timed out after %.0fs in guild %s",
                _PLAY_TIMEOUT_SECONDS,
                guild.id,
            )
            # Force-disconnect on timeout.
            await self._safe_disconnect(guild)
            return False
        except Exception:
            logger.exception(
                "Unexpected error during voice alert in guild %s", guild.id
            )
            await self._safe_disconnect(guild)
            return False

    async def _play_locked(
        self,
        lock: asyncio.Lock,
        channel: discord.VoiceChannel,
        audio_path: str,
    ) -> bool:
        async with lock:
            return await self._play_inner(channel, audio_path)

    async def _play_inner(
        self,
        channel: discord.VoiceChannel,
        audio_path: str,
    ) -> bool:
        guild = channel.guild
        voice_client: Optional[discord.VoiceClient] = None

        logger.info("Voice alert started in guild %s for channel %s", guild.id, channel.name)

        try:
            # If the bot is already connected in this guild, DO NOT disconnect it.
            # Skip the voice alert and fall back to normal flow.
            if guild.voice_client is not None:
                logger.info(
                    "Bot is already connected in guild %s. Voice alert fallback activated.",
                    guild.id,
                )
                return False

            voice_client = await channel.connect(timeout=5.0, self_deaf=True)
        except discord.Forbidden:
            logger.warning(
                "Missing voice permissions for channel %s in guild %s. Voice alert fallback activated.",
                channel.name,
                guild.id,
            )
            return False
        except discord.ClientException as exc:
            logger.warning(
                "ClientException connecting to %s in guild %s: %s. Voice alert fallback activated.",
                channel.name,
                guild.id,
                exc,
            )
            return False
        except Exception:
            logger.exception(
                "Failed to connect to voice channel %s in guild %s. Voice alert fallback activated.",
                channel.name,
                guild.id,
            )
            return False

        try:
            played = await self._play_audio(voice_client, audio_path)
            if played:
                logger.info("Voice alert completed successfully in guild %s", guild.id)
            else:
                logger.warning("Voice alert fallback activated in guild %s due to playback failure", guild.id)
            return played
        finally:
            await self._safe_disconnect(guild)

    async def _play_audio(
        self,
        voice_client: discord.VoiceClient,
        audio_path: str,
    ) -> bool:
        """Play *audio_path* on *voice_client* and wait until done."""
        loop = asyncio.get_running_loop()
        finished = loop.create_future()

        def _after(error: Exception | None) -> None:
            if error is not None:
                logger.error("FFmpeg playback error: %s", error)
            if not finished.done():
                loop.call_soon_threadsafe(finished.set_result, error is None)

        try:
            source = discord.FFmpegPCMAudio(audio_path)
        except Exception:
            logger.exception("Failed to create FFmpegPCMAudio source")
            return False

        try:
            voice_client.play(source, after=_after)
        except discord.ClientException:
            logger.exception("Failed to start playback (already playing?)")
            return False

        return await finished

    @staticmethod
    async def _safe_disconnect(guild: discord.Guild) -> None:
        """Disconnect the bot from voice in *guild*, swallowing errors."""
        vc = guild.voice_client
        if vc is None:
            return
        try:
            await vc.disconnect(force=True)
            logger.info("Voice alert cleanup completed: disconnected from voice in guild %s", guild.id)
        except Exception:
            logger.exception(
                "Error disconnecting voice client in guild %s", guild.id
            )
