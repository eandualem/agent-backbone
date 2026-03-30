"""Tests for api/session_updates.py."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import agent_backbone.api.session_updates as session_updates_module
from agent_backbone.api.models import EnrichedAgent
from agent_backbone.api.session_updates import (
    SESSIONS_NAMESPACE,
    SESSIONS_UPDATE_EVENT,
    build_enriched_agent,
    emit_sessions_update,
    get_cached_session_snapshot,
    invalidate_session_snapshot_caches,
    reset_sessions_update_state,
)
from agent_backbone.services.agents.models import AgentState, StateSnapshot


@pytest.fixture(autouse=True)
def _reset_update_state():
    reset_sessions_update_state()
    yield
    reset_sessions_update_state()


def _sample_snapshot() -> list[EnrichedAgent]:
    return [
        EnrichedAgent(
            session="agent-backbone",
            entity="agent-backbone",
            state="idle",
            online=True,
            type="coding_agent",
            org="WF",
            groups=["orchestrators"],
        )
    ]


class TestGetCachedSessionSnapshot:
    @pytest.mark.asyncio
    async def test_ttl_hit_returns_cached_snapshot(self):
        """Repeated reads inside the TTL reuse the cached snapshot."""
        first_snapshot = _sample_snapshot()
        second_snapshot = [
            EnrichedAgent(
                session="other-repo",
                entity="other-repo",
                state="busy",
                online=True,
                type="coding_agent",
            )
        ]
        build_fn = AsyncMock(side_effect=[first_snapshot, second_snapshot])

        first = await get_cached_session_snapshot(build_fn)
        second = await get_cached_session_snapshot(build_fn)

        assert first == first_snapshot
        assert second == first_snapshot
        assert build_fn.await_count == 1

    @pytest.mark.asyncio
    async def test_ttl_miss_rebuilds_snapshot(self):
        """Expired TTL triggers a new snapshot build."""
        first_snapshot = _sample_snapshot()
        second_snapshot = [
            EnrichedAgent(
                session="other-repo",
                entity="other-repo",
                state="busy",
                online=True,
                type="coding_agent",
            )
        ]
        build_fn = AsyncMock(side_effect=[first_snapshot, second_snapshot])

        first = await get_cached_session_snapshot(build_fn)
        second = await get_cached_session_snapshot(build_fn, ttl=0)

        assert first == first_snapshot
        assert second == second_snapshot
        assert build_fn.await_count == 2

    @pytest.mark.asyncio
    async def test_force_refresh_rebuilds_snapshot(self):
        """Force refresh bypasses the cache even within the TTL."""
        first_snapshot = _sample_snapshot()
        second_snapshot = [
            EnrichedAgent(
                session="other-repo",
                entity="other-repo",
                state="busy",
                online=True,
                type="coding_agent",
            )
        ]
        build_fn = AsyncMock(side_effect=[first_snapshot, second_snapshot])

        first = await get_cached_session_snapshot(build_fn)
        second = await get_cached_session_snapshot(build_fn, force_refresh=True)

        assert first == first_snapshot
        assert second == second_snapshot
        assert build_fn.await_count == 2

    @pytest.mark.asyncio
    async def test_invalidate_resets_cache(self):
        """Invalidation clears the shared cached snapshot state."""
        snapshot = _sample_snapshot()
        build_fn = AsyncMock(return_value=snapshot)

        await get_cached_session_snapshot(build_fn)
        assert session_updates_module._snapshot_cache == snapshot
        assert session_updates_module._snapshot_cache_ts > 0

        await invalidate_session_snapshot_caches()

        assert session_updates_module._snapshot_cache == []
        assert session_updates_module._snapshot_cache_ts == 0.0

    @pytest.mark.asyncio
    async def test_concurrent_calls_share_one_build_under_lock(self):
        """Concurrent cache misses are serialized under the shared lock."""
        snapshot = _sample_snapshot()

        async def slow_build():
            await asyncio.sleep(0.05)
            return snapshot

        build_fn = AsyncMock(side_effect=slow_build)

        first, second, third = await asyncio.gather(
            get_cached_session_snapshot(build_fn),
            get_cached_session_snapshot(build_fn),
            get_cached_session_snapshot(build_fn),
        )

        assert first == snapshot
        assert second == snapshot
        assert third == snapshot
        assert build_fn.await_count == 1

    @pytest.mark.asyncio
    async def test_invalidation_waits_for_inflight_rebuild(self):
        """Invalidation must not be overwritten by a rebuild that already holds the lock."""
        snapshot = _sample_snapshot()
        build_started = asyncio.Event()
        release_build = asyncio.Event()

        async def slow_build():
            build_started.set()
            await release_build.wait()
            return snapshot

        rebuild_task = asyncio.create_task(
            get_cached_session_snapshot(AsyncMock(side_effect=slow_build), force_refresh=True)
        )

        await build_started.wait()
        invalidate_task = asyncio.create_task(invalidate_session_snapshot_caches())
        await asyncio.sleep(0)
        assert invalidate_task.done() is False

        release_build.set()
        await rebuild_task
        await invalidate_task

        assert session_updates_module._snapshot_cache == []
        assert session_updates_module._snapshot_cache_ts == 0.0


class TestEmitSessionsUpdate:
    @pytest.mark.asyncio
    async def test_noops_without_services(self):
        """Missing server or services skips broadcasting cleanly."""
        assert await emit_sessions_update(None, MagicMock(), MagicMock(), MagicMock()) is False
        assert await emit_sessions_update(MagicMock(), MagicMock(), None, MagicMock()) is False
        assert await emit_sessions_update(MagicMock(), MagicMock(), MagicMock(), None) is False

    @pytest.mark.asyncio
    async def test_emits_to_all_agents_agent_org_and_group_rooms(self):
        """Changed agents emit to all-agents plus targeted session-set rooms."""
        sio = MagicMock()
        sio.emit = AsyncMock()

        with patch(
            "agent_backbone.api.session_updates.get_cached_session_snapshot",
            new_callable=AsyncMock,
            return_value=_sample_snapshot(),
        ):
            emitted = await emit_sessions_update(sio, MagicMock(), MagicMock(), MagicMock())

        assert emitted is True
        agent_dict = _sample_snapshot()[0].model_dump(mode="json")
        # SUB-8: all-agents room gets full changed list
        sio.emit.assert_any_await(
            SESSIONS_UPDATE_EVENT,
            [agent_dict],
            namespace=SESSIONS_NAMESPACE,
            room="all-agents",
        )
        # SUB-8: per-agent room gets single-element list
        sio.emit.assert_any_await(
            SESSIONS_UPDATE_EVENT,
            [agent_dict],
            namespace=SESSIONS_NAMESPACE,
            room="agent:agent-backbone",
        )
        sio.emit.assert_any_await(
            SESSIONS_UPDATE_EVENT,
            [agent_dict],
            namespace=SESSIONS_NAMESPACE,
            room="org:WF",
        )
        sio.emit.assert_any_await(
            SESSIONS_UPDATE_EVENT,
            [agent_dict],
            namespace=SESSIONS_NAMESPACE,
            room="group:orchestrators",
        )

    @pytest.mark.asyncio
    async def test_per_agent_change_detection_suppresses_unchanged(self):
        """SUB-9: unchanged agents are not re-emitted."""
        sio = MagicMock()
        sio.emit = AsyncMock()

        with patch(
            "agent_backbone.api.session_updates.get_cached_session_snapshot",
            new_callable=AsyncMock,
            return_value=_sample_snapshot(),
        ):
            first = await emit_sessions_update(sio, MagicMock(), MagicMock(), MagicMock())
            second = await emit_sessions_update(sio, MagicMock(), MagicMock(), MagicMock())

        assert first is True
        assert second is False
        # Only emitted on first call (4 calls: all-agents + agent + org + group)
        assert sio.emit.await_count == 4


class TestBuildEnrichedAgent:
    @pytest.mark.asyncio
    async def test_offline_session_uses_reported_state_without_db_sync(self):
        """Offline snapshot building must use the reported DB state without a live refresh."""
        config = MagicMock()
        config.registry.entry_for_session.return_value = None
        config.registry.entities.get.return_value = None
        config.registry.repo_path_by_name = {}
        config.registry.repos = []

        state_svc = MagicMock()
        state_svc.get_reported_state = AsyncMock(
            return_value=StateSnapshot(
                state=AgentState.IDLE,
                current_issue=42,
                timestamp=123.0,
                source="db",
            )
        )
        state_svc.get_state = AsyncMock()

        agent = await build_enriched_agent(
            session="ada",
            entity="ada",
            config=config,
            active_sessions=set(),
            state_svc=state_svc,
            agent_type="coding_agent",
        )

        state_svc.get_reported_state.assert_awaited_once_with("ada")
        state_svc.get_state.assert_not_awaited()
        assert agent.state == "offline"
        assert agent.current_issue == 42

    @pytest.mark.asyncio
    async def test_online_offline_state_remains_offline(self):
        """Reported offline state stays offline for online sessions."""
        config = MagicMock()
        config.registry.entry_for_session.return_value = None
        config.registry.entities.get.return_value = None
        config.registry.repo_path_by_name = {}
        config.registry.repos = []

        state_svc = MagicMock()
        state_svc.get_state = AsyncMock(
            return_value=StateSnapshot(
                state=AgentState.OFFLINE,
                timestamp=123.0,
                source="pull",
            )
        )

        agent = await build_enriched_agent(
            session="agent-backbone",
            entity="agent-backbone",
            config=config,
            active_sessions={"agent-backbone"},
            state_svc=state_svc,
            agent_type="coding_agent",
        )

        assert agent.online is True
        assert agent.state == "offline"

    @pytest.mark.asyncio
    async def test_permission_context_is_exposed_on_enriched_agent(self):
        """Structured state context survives into the Socket.IO session payload."""
        config = MagicMock()
        config.registry.entry_for_session.return_value = None
        config.registry.entities.get.return_value = None
        config.registry.repo_path_by_name = {}
        config.registry.repos = []

        state_svc = MagicMock()
        state_svc.get_state = AsyncMock(
            return_value=StateSnapshot(
                state=AgentState.PERMISSION_WAITING,
                timestamp=123.0,
                source="observed",
                context={
                    "tool": "Read",
                    "target": "/tmp/example",
                    "prompt": "Do you want to proceed?",
                },
            )
        )

        agent = await build_enriched_agent(
            session="agent-backbone",
            entity="agent-backbone",
            config=config,
            active_sessions={"agent-backbone"},
            state_svc=state_svc,
            agent_type="coding_agent",
        )

        assert agent.context == {
            "tool": "Read",
            "target": "/tmp/example",
            "prompt": "Do you want to proceed?",
        }
