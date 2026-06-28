"""Tests for the VoiceAlertService."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from services.voice_alert import VoiceAlertService, _PLAY_TIMEOUT_SECONDS


@pytest.fixture(autouse=True)
def fast_tests():
    with patch("services.voice_alert._POST_PLAYBACK_DELAY", 0.0):
        yield


@pytest.fixture
def mock_channel() -> MagicMock:
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.name = "Solo #1"
    
    guild = MagicMock(spec=discord.Guild)
    guild.id = 123
    guild.voice_client = None  # Not connected by default
    channel.guild = guild
    
    return channel


@pytest.fixture
def mock_voice_client() -> AsyncMock:
    vc = AsyncMock(spec=discord.VoiceClient)
    vc.is_connected.return_value = True
    vc.guild = MagicMock(spec=discord.Guild)
    vc.guild.id = 123
    return vc


@pytest.mark.asyncio
async def test_play_alert_success(mock_channel: MagicMock, mock_voice_client: AsyncMock) -> None:
    """Test successful voice alert playback."""
    service = VoiceAlertService("dummy.mp3")
    guild = mock_channel.guild

    async def _connect(**kwargs: object) -> AsyncMock:
        # Simulate discord.py setting guild.voice_client after connect.
        guild.voice_client = mock_voice_client
        return mock_voice_client

    mock_channel.connect.side_effect = _connect

    # Mock play_audio to just return True immediately
    with patch.object(service, "_play_audio", return_value=True):
        with patch("os.path.isfile", return_value=True):
            result = await service.play_alert(mock_channel)
            
    assert result is True
    mock_channel.connect.assert_called_once()
    mock_voice_client.disconnect.assert_called_once_with(force=True)


@pytest.mark.asyncio
async def test_play_alert_already_connected(mock_channel: MagicMock) -> None:
    """Test fallback when bot is already connected in the guild."""
    service = VoiceAlertService("dummy.mp3")
    
    # Simulate already connected
    existing_vc = AsyncMock(spec=discord.VoiceClient)
    mock_channel.guild.voice_client = existing_vc
    
    with patch("os.path.isfile", return_value=True):
        result = await service.play_alert(mock_channel)
        
    assert result is False
    # Should not connect again, and should not disconnect the existing connection!
    mock_channel.connect.assert_not_called()
    existing_vc.disconnect.assert_not_called()


@pytest.mark.asyncio
async def test_play_alert_missing_permissions(mock_channel: MagicMock) -> None:
    """Test fallback when bot lacks voice permissions."""
    service = VoiceAlertService("dummy.mp3")
    mock_channel.connect.side_effect = discord.Forbidden(
        MagicMock(status=403), "Missing Permissions"
    )
    
    with patch("os.path.isfile", return_value=True):
        result = await service.play_alert(mock_channel)
        
    assert result is False
    mock_channel.connect.assert_called_once()


@pytest.mark.asyncio
async def test_play_alert_timeout(mock_channel: MagicMock, mock_voice_client: AsyncMock) -> None:
    """Test that a long playback times out and forces disconnect."""
    service = VoiceAlertService("dummy.mp3")
    guild = mock_channel.guild

    async def _connect(**kwargs: object) -> AsyncMock:
        guild.voice_client = mock_voice_client
        return mock_voice_client

    mock_channel.connect.side_effect = _connect
    
    # Simulate play_audio hanging
    async def hanging_play(*args: object, **kwargs: object) -> bool:
        await asyncio.sleep(0.2)
        return True
    # Use a shorter timeout just for the test to avoid waiting 15s
    with patch("services.voice_alert._PLAY_TIMEOUT_SECONDS", 0.1):
        with patch.object(service, "_play_audio", side_effect=hanging_play):
            with patch("os.path.isfile", return_value=True):
                result = await service.play_alert(mock_channel)
                
    assert result is False
    # disconnect is called from both _play_inner finally and play_alert's
    # _safe_disconnect fallback — verify at least one call happened.
    assert mock_voice_client.disconnect.call_count >= 1


@pytest.mark.asyncio
async def test_play_alert_cancellation_disconnects(
    mock_channel: MagicMock, mock_voice_client: AsyncMock
) -> None:
    """When play_alert is cancelled, the bot must still disconnect."""
    service = VoiceAlertService("dummy.mp3")
    guild = mock_channel.guild

    async def _connect(**kwargs: object) -> AsyncMock:
        guild.voice_client = mock_voice_client
        return mock_voice_client

    mock_channel.connect.side_effect = _connect

    async def slow_play(*args: object, **kwargs: object) -> bool:
        await asyncio.sleep(10)
        return True

    with patch.object(service, "_play_audio", side_effect=slow_play):
        with patch("os.path.isfile", return_value=True):
            task = asyncio.create_task(service.play_alert(mock_channel))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    # Verify disconnect was called despite cancellation.
    assert mock_voice_client.disconnect.call_count >= 1


@pytest.mark.asyncio
async def test_play_alert_exception_during_playback_disconnects(
    mock_channel: MagicMock, mock_voice_client: AsyncMock
) -> None:
    """When _play_audio raises an unexpected error, the bot must still disconnect."""
    service = VoiceAlertService("dummy.mp3")
    guild = mock_channel.guild

    async def _connect(**kwargs: object) -> AsyncMock:
        guild.voice_client = mock_voice_client
        return mock_voice_client

    mock_channel.connect.side_effect = _connect

    with patch.object(service, "_play_audio", side_effect=RuntimeError("ffmpeg crash")):
        with patch("os.path.isfile", return_value=True):
            result = await service.play_alert(mock_channel)

    assert result is False
    # The finally block in _play_inner disconnects, and the exception handler
    # in play_alert calls _safe_disconnect as a second defense.
    assert mock_voice_client.disconnect.call_count >= 1


@pytest.mark.asyncio
async def test_play_alert_connect_failure_does_not_disconnect(mock_channel: MagicMock) -> None:
    """When connect itself fails, no disconnect should be attempted."""
    service = VoiceAlertService("dummy.mp3")
    mock_channel.connect.side_effect = RuntimeError("connection failed")

    with patch("os.path.isfile", return_value=True):
        result = await service.play_alert(mock_channel)

    assert result is False
    # guild.voice_client is still None → _safe_disconnect is a no-op
    # and _play_inner's finally never runs because voice_client is None.
