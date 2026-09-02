"""Tests for the session feed (api/session_updates.py)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from agent_backbone.api.models import EnrichedAgent
from agent_backbone.api.session_updates import (
    SESSIONS_NAMESPACE,
    SESSIONS_UPDATE_EVENT,
    SessionFeed,
)

_BUILD = "agent_backbone.api.session_updates.build_session_snapshot"


def _agent(name: str = "agent-backbone", state: str = "idle") -> EnrichedAgent:
    return EnrichedAgent(session=name, name=name, state=state, online=True)


def _feed(sio=None, **kwargs) -> SessionFeed:
    return SessionFeed(lambda: MagicMock(), sio, **kwargs)


class TestSnapshot:
    async def test_ttl_hit_returns_cached_snapshot(self):
        feed = _feed()
        with patch(_BUILD, AsyncMock(side_effect=[[_agent()], [_agent("other")]])) as build:
            first = await feed.snapshot()
            second = await feed.snapshot()
        assert first == second == [_agent()]
        assert build.await_count == 1

    async def test_ttl_miss_rebuilds_snapshot(self):
        feed = _feed(ttl_seconds=0)
        with patch(_BUILD, AsyncMock(side_effect=[[_agent()], [_agent("other")]])) as build:
            first = await feed.snapshot()
            second = await feed.snapshot()
        assert first == [_agent()] and second == [_agent("other")]
        assert build.await_count == 2

    async def test_force_refresh_rebuilds_snapshot(self):
        feed = _feed()
        with patch(_BUILD, AsyncMock(side_effect=[[_agent()], [_agent("other")]])) as build:
            await feed.snapshot()
            second = await feed.snapshot(force_refresh=True)
        assert second == [_agent("other")]
        assert build.await_count == 2

    async def test_invalidate_forgets_the_cache(self):
        feed = _feed()
        with patch(_BUILD, AsyncMock(return_value=[_agent()])) as build:
            await feed.snapshot()
            await feed.invalidate()
            await feed.snapshot()
        assert build.await_count == 2

    async def test_concurrent_misses_share_one_build(self):
        feed = _feed()

        async def slow_build(config):
            await asyncio.sleep(0.05)
            return [_agent()]

        with patch(_BUILD, AsyncMock(side_effect=slow_build)) as build:
            results = await asyncio.gather(feed.snapshot(), feed.snapshot(), feed.snapshot())
        assert all(r == [_agent()] for r in results)
        assert build.await_count == 1

    async def test_invalidation_waits_for_an_inflight_rebuild(self):
        feed = _feed()
        build_started = asyncio.Event()
        release_build = asyncio.Event()

        async def slow_build(config):
            build_started.set()
            await release_build.wait()
            return [_agent()]

        with patch(_BUILD, AsyncMock(side_effect=slow_build)):
            rebuild = asyncio.create_task(feed.snapshot(force_refresh=True))
            await build_started.wait()
            invalidate = asyncio.create_task(feed.invalidate())
            await asyncio.sleep(0)
            assert invalidate.done() is False
            release_build.set()
            await rebuild
            await invalidate
        assert feed._cache == []


class TestEmit:
    async def test_noops_without_a_server(self):
        assert await _feed(None).emit() is False

    async def test_emits_snapshot_payload(self):
        sio = MagicMock()
        sio.emit = AsyncMock()
        feed = _feed(sio)
        with patch(_BUILD, AsyncMock(return_value=[_agent()])):
            assert await feed.emit() is True
        sio.emit.assert_awaited_once_with(
            SESSIONS_UPDATE_EVENT, [_agent().model_dump(mode="json")], namespace=SESSIONS_NAMESPACE
        )

    async def test_only_if_changed_suppresses_duplicate_payload(self):
        sio = MagicMock()
        sio.emit = AsyncMock()
        feed = _feed(sio)
        with patch(_BUILD, AsyncMock(return_value=[_agent()])) as build:
            first = await feed.emit(only_if_changed=True)
            second = await feed.emit(only_if_changed=True)
        assert first is True and second is False
        assert build.await_count == 2  # always rebuilt; only the broadcast is suppressed
        sio.emit.assert_awaited_once()

    async def test_refresh_and_emit_drops_the_cache_first(self):
        sio = MagicMock()
        sio.emit = AsyncMock()
        feed = _feed(sio)
        with patch(_BUILD, AsyncMock(side_effect=[[_agent()], [_agent("other")]])):
            await feed.snapshot()
            await feed.refresh_and_emit()
        assert sio.emit.await_args.args[1] == [_agent("other").model_dump(mode="json")]
