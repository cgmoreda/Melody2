import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.verification_manager import VerificationManager, VerificationSession


@pytest.fixture
def get_interval():
    async def _get(guild_id: int, channel_type: str) -> tuple[int, int]:
        return 10, 10
    return _get


@pytest.fixture
def random_fn():
    def _rand(min_val: int, max_val: int) -> int:
        return min_val
    return _rand


@pytest.fixture
def on_due():
    return AsyncMock(return_value=True)


@pytest.fixture
def manager(get_interval, on_due, random_fn):
    return VerificationManager(get_interval, on_due, random_fn)


@pytest.mark.asyncio
async def test_evaluate_channel_schedules_solo(manager):
    await manager.evaluate_channel(
        guild_id=1,
        channel_id=101,
        channel_type="solo",
        human_member_ids=[2, 3],
    )
    assert manager.is_scheduled(2)
    assert manager.is_scheduled(3)
    assert manager.metrics.scheduled == 2


@pytest.mark.asyncio
async def test_evaluate_channel_schedules_duo_when_alone(manager):
    await manager.evaluate_channel(
        guild_id=1,
        channel_id=102,
        channel_type="duo",
        human_member_ids=[2],
    )
    assert manager.is_scheduled(2)
    assert manager.metrics.scheduled == 1


@pytest.mark.asyncio
async def test_evaluate_channel_cancels_duo_when_not_alone(manager):
    # Setup single member
    await manager.evaluate_channel(
        guild_id=1,
        channel_id=102,
        channel_type="duo",
        human_member_ids=[2],
    )
    assert manager.is_scheduled(2)

    # Second member joins
    await manager.evaluate_channel(
        guild_id=1,
        channel_id=102,
        channel_type="duo",
        human_member_ids=[2, 3],
    )
    assert not manager.is_scheduled(2)
    assert not manager.is_scheduled(3)
    assert manager.metrics.cancelled == 1


@pytest.mark.asyncio
async def test_evaluate_channel_idempotent(manager):
    await manager.evaluate_channel(
        guild_id=1,
        channel_id=101,
        channel_type="solo",
        human_member_ids=[2],
    )
    assert manager.metrics.scheduled == 1

    # Call again with same state
    await manager.evaluate_channel(
        guild_id=1,
        channel_id=101,
        channel_type="solo",
        human_member_ids=[2],
    )
    # Should not schedule again or cancel
    assert manager.metrics.scheduled == 1
    assert manager.metrics.cancelled == 0


@pytest.mark.asyncio
async def test_timer_loop_generation_mismatch(manager):
    await manager.evaluate_channel(
        guild_id=1,
        channel_id=101,
        channel_type="solo",
        human_member_ids=[2],
    )
    session = manager._sessions[2]

    # Simulate a new session replacing the old one
    new_session = VerificationSession(
        member_id=2, guild_id=1, channel_id=101, channel_type="solo",
        created_at=session.created_at, generation=session.generation + 1
    )
    manager._sessions[2] = new_session

    # Run the timer loop manually with the OLD session
    await manager._timer_loop(session, 0)

    # Callback should not have been called
    manager._on_verification_due.assert_not_called()
    assert manager.metrics.completed == 0


@pytest.mark.asyncio
async def test_timer_loop_reschedules_on_success(manager, on_due):
    on_due.return_value = True

    await manager.evaluate_channel(
        guild_id=1,
        channel_id=101,
        channel_type="solo",
        human_member_ids=[2],
    )
    session = manager._sessions[2]

    # Run loop manually (delay=0)
    await manager._timer_loop(session, 0)

    on_due.assert_called_once_with(1, 2, 101, "solo")
    assert manager.metrics.completed == 1
    assert manager.metrics.verification_passed == 1
    # Should schedule a new task
    assert manager.metrics.scheduled == 2


@pytest.mark.asyncio
async def test_timer_loop_cancels_on_failure(manager, on_due):
    on_due.return_value = False

    await manager.evaluate_channel(
        guild_id=1,
        channel_id=101,
        channel_type="solo",
        human_member_ids=[2],
    )
    session = manager._sessions[2]

    # Run loop manually
    await manager._timer_loop(session, 0)

    on_due.assert_called_once_with(1, 2, 101, "solo")
    assert manager.metrics.completed == 1
    assert manager.metrics.verification_failed == 1
    assert not manager.is_scheduled(2)
