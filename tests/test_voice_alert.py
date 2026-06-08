"""Tests for the VoiceAlertService."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from services.voice_alert import VoiceAlertService, _PLAY_TIMEOUT_SECONDS


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
    return vc


@pytest.mark.asyncio
async def test_play_alert_success(mock_channel: MagicMock, mock_voice_client: AsyncMock) -> None:
    """Test successful voice alert playback."""
    service = VoiceAlertService("dummy.mp3")
    mock_channel.connect.return_value = mock_voice_client
    
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
    mock_channel.connect.return_value = mock_voice_client
    
    # Simulate play_audio hanging
    async def hanging_play(*args, **kwargs) -> bool:
        await asyncio.sleep(_PLAY_TIMEOUT_SECONDS + 1.0)
        return True

    # Use a shorter timeout just for the test to avoid waiting 15s
    with patch("services.voice_alert._PLAY_TIMEOUT_SECONDS", 0.1):
        with patch.object(service, "_play_audio", side_effect=hanging_play):
            with patch("os.path.isfile", return_value=True):
                result = await service.play_alert(mock_channel)
                
    assert result is False
    mock_voice_client.disconnect.assert_called_once_with(force=True)
