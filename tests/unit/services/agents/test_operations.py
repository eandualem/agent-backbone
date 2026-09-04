"""The mutations every surface shares: stop and forget carry their guards here."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.services.agents.operations import forget_agent, stop_agent_session

_OPS = "agent_backbone.services.agents.operations"


async def test_stop_refuses_the_backbones_own_session(config):
    with patch(f"{_OPS}.launch.stop_agent", new_callable=AsyncMock) as stop:
        with pytest.raises(ValueError):
            await stop_agent_session(config, config.backbone.session_name)
    stop.assert_not_called()


async def test_stop_stops_an_agent(config):
    with patch(f"{_OPS}.launch.stop_agent", new_callable=AsyncMock, return_value=True) as stop:
        assert await stop_agent_session(config, "ike") is True
    stop.assert_awaited_once_with("ike")


async def test_forget_refuses_a_running_agent():
    store = AsyncMock()
    with patch(f"{_OPS}.session_exists", new_callable=AsyncMock, return_value=True):
        with pytest.raises(RuntimeError):
            await forget_agent(store, "ike")
    store.forget.assert_not_called()


async def test_forget_removes_a_stopped_agent():
    store = AsyncMock()
    store.forget.return_value = True
    with patch(f"{_OPS}.session_exists", new_callable=AsyncMock, return_value=False):
        assert await forget_agent(store, "ike") is True
    store.forget.assert_awaited_once_with("ike")


async def test_forget_waits_for_a_start_in_progress_and_then_refuses():
    """The race the review named: a start launching between the check and the delete."""
    import asyncio

    from agent_backbone.services.agents.operations import lifecycle_lock

    store = AsyncMock()
    store.forget.return_value = True
    running = False

    async def _exists(name):
        return running

    with patch(f"{_OPS}.session_exists", side_effect=_exists):
        async with lifecycle_lock("ike"):  # a start holds the lock
            task = asyncio.create_task(forget_agent(store, "ike"))
            await asyncio.sleep(0)
            assert not task.done()  # forget is waiting for the start to finish
            running = True  # the start brought the session up
        with pytest.raises(RuntimeError):
            await task
    store.forget.assert_not_called()
