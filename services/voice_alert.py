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
_PLAY_TIMEOUT_SECONDS = 30.0

# Time to wait after playback finishes to allow buffered audio packets to flush.
_POST_PLAYBACK_DELAY = 2.5



def _create_audio_source(audio_path: str) -> discord.AudioSource:
    """Create an audio source, preferring OpusAudio over PCMAudio.

    ``FFmpegOpusAudio`` lets FFmpeg encode directly to Opus, which
    removes the requirement for ``libopus`` to be installed on the
    host system.  ``FFmpegPCMAudio`` is kept as a fallback in case
    ``FFmpegOpusAudio`` fails to initialise for any reason.
    """
    try:
        source = discord.FFmpegOpusAudio(audio_path)
        logger.debug("Using FFmpegOpusAudio for %s", audio_path)
        return source
    except Exception:
        logger.warning(
            "FFmpegOpusAudio unavailable, falling back to FFmpegPCMAudio for %s",
            audio_path,
            exc_info=True,
        )
    source = discord.FFmpegPCMAudio(audio_path)
    logger.debug("Using FFmpegPCMAudio for %s", audio_path)
    return source


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
            await self._safe_disconnect(guild)
            return False
        except asyncio.CancelledError:
            await self._safe_disconnect(guild)
            raise
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

        # If the bot is already connected in this guild, DO NOT disconnect it.
        # Skip the voice alert and fall back to normal flow.
        if guild.voice_client is not None:
            logger.info(
                "Bot is already connected in guild %s. Voice alert fallback activated.",
                guild.id,
            )
            return False

        logger.info(
            "[VOICE_CONNECT_START] guild=%s channel=%s",
            guild.id,
            channel.name,
        )

        try:
            voice_client = await channel.connect(timeout=15.0, self_deaf=True)
        except discord.Forbidden:
            logger.warning(
                "[VOICE_CONNECT_START] FAILED — missing voice permissions for channel %s in guild %s. Fallback activated.",
                channel.name,
                guild.id,
            )
            return False
        except discord.ClientException as exc:
            logger.warning(
                "[VOICE_CONNECT_START] FAILED — ClientException for %s in guild %s: %s. Fallback activated.",
                channel.name,
                guild.id,
                exc,
            )
            return False
        except Exception:
            logger.exception(
                "[VOICE_CONNECT_START] FAILED — exception connecting to %s in guild %s. Fallback activated.",
                channel.name,
                guild.id,
            )
            return False

        logger.info(
            "[VOICE_CONNECT_OK] guild=%s channel=%s",
            guild.id,
            channel.name,
        )

        try:
            played = await self._play_audio(voice_client, audio_path)
            if played:
                logger.info("[PLAYBACK_DONE] success — guild=%s", guild.id)
                await asyncio.sleep(_POST_PLAYBACK_DELAY)
            else:
                logger.warning("[PLAYBACK_DONE] failed — guild=%s. Fallback activated.", guild.id)
            return played
        finally:
            if voice_client is not None:
                logger.info("[CLEANUP_START] guild=%s", guild.id)
                try:
                    if voice_client.is_connected():
                        await voice_client.disconnect(force=True)
                    logger.info("[CLEANUP_DONE] guild=%s", guild.id)
                except Exception:
                    logger.exception(
                        "[CLEANUP_DONE] error disconnecting voice client in guild %s", guild.id
                    )
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
            source = _create_audio_source(audio_path)
        except Exception:
            logger.exception("Failed to create audio source for %s", audio_path)
            return False

        try:
            await asyncio.sleep(1.0)
            logger.info("[PLAYBACK_START] guild=%s file=%s", voice_client.guild.id, audio_path)
            voice_client.play(source, after=_after)
        except discord.ClientException:
            logger.exception("[PLAYBACK_START] FAILED — already playing? guild=%s", voice_client.guild.id)
            return False

        return await finished

    @staticmethod
    async def _safe_disconnect(guild: discord.Guild) -> None:
        """Disconnect the bot from voice in *guild*, swallowing errors."""
        vc = guild.voice_client
        if vc is None:
            return
        logger.info("[CLEANUP_START] guild=%s", guild.id)
        try:
            if vc.is_connected():
                await vc.disconnect()
            logger.info("[CLEANUP_DONE] guild=%s", guild.id)
        except Exception:
            logger.exception(
                "[CLEANUP_DONE] error disconnecting voice client in guild %s", guild.id
            )
