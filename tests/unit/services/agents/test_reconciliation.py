"""Tests for startup state reconciliation."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.config import (
    AgentStateConfig,
    BackboneConfig,
    CapacityRoutingConfig,
    DeliveryConfig,
    EntityConfig,
    EscalationConfig,
    GitHubConfig,
)
from agent_backbone.services.agents import (
    AgentState,
    StateSnapshot,
    _plan_notify_dedup,
)
from agent_backbone.services.agents._reconciliation import reconcile_startup_states
from agent_backbone.services.registry import EntityEntry, EntityRegistry

_REC = "agent_backbone.services.agents._reconciliation"


def _make_registry(names: list[str]) -> EntityRegistry:
    return EntityRegistry(
        entities={
            n: EntityEntry(session=n, home=f"~/ws/{n}", groups=[], figure="", role="")
            for n in names
        },
        repos=[],
    )


@pytest.fixture(autouse=True)
def clear_plan_dedup():
    _plan_notify_dedup.clear()
    yield
    _plan_notify_dedup.clear()


@pytest.fixture
def config():
    return BackboneConfig(
        github_token="test-token",
        webhook_secret="test-secret",
        github=GitHubConfig(owner="eandualem", repo="orchestration"),
        entities=EntityConfig(skip=frozenset({"elias"})),
        registry=_make_registry(["ike", "feynman"]),
        agent_state=AgentStateConfig(
            state_dir="/tmp/test-state",
            stale_threshold_seconds=300,
        ),
        escalation=EscalationConfig(
            stall_threshold_seconds=5400,
            escalation_target="ike",
            escalation_dedup_seconds=1800,
        ),
        delivery=DeliveryConfig(),
        capacity_routing=CapacityRoutingConfig(busy_threshold_seconds=1800),
    )


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.set_agent_state = AsyncMock()
    return db


class TestReconcileStartupStates:
    @pytest.mark.asyncio
    async def test_no_sessions_early_return(self, config, mock_db):
        """No tmux sessions → early return, no DB writes."""
        with patch(f"{_REC}.list_sessions", new_callable=AsyncMock, return_value=[]):
            await reconcile_startup_states(config, mock_db)
        mock_db.set_agent_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_syncs_active_entity_states(self, config, mock_db):
        """Both entities with active sessions get synced to DB."""
        idle_snap = StateSnapshot(state=AgentState.IDLE, source="push")
        busy_snap = StateSnapshot(state=AgentState.BUSY, current_issue=42, source="push")

        async def mock_get_state(state_path, session, stale):
            return idle_snap if session == "ike" else busy_snap

        with (
            patch(f"{_REC}.list_sessions", new_callable=AsyncMock, return_value=["ike", "feynman"]),
            patch(f"{_REC}.get_agent_state", side_effect=mock_get_state),
            patch(f"{_REC}.check_plan_waiting", new_callable=AsyncMock),
        ):
            await reconcile_startup_states(config, mock_db)

        assert mock_db.set_agent_state.call_count == 2

    @pytest.mark.asyncio
    async def test_skips_inactive_sessions(self, config, mock_db):
        """Entities without active sessions are skipped."""
        idle_snap = StateSnapshot(state=AgentState.IDLE, source="push")

        with (
            patch(f"{_REC}.list_sessions", new_callable=AsyncMock, return_value=["ike"]),
            patch(f"{_REC}.get_agent_state", new_callable=AsyncMock, return_value=idle_snap),
            patch(f"{_REC}.check_plan_waiting", new_callable=AsyncMock),
        ):
            await reconcile_startup_states(config, mock_db)

        # Only ike synced, feynman skipped (no active session)
        assert mock_db.set_agent_state.call_count == 1
        mock_db.set_agent_state.assert_called_once_with(
            session_name="ike",
            state="idle",
            current_issue=None,
            entity="ike",
            plan_file=None,
            plan_title=None,
        )

    @pytest.mark.asyncio
    async def test_plan_waiting_state_synced_with_fields(self, config, mock_db):
        """plan_waiting state includes plan_file and plan_title."""
        plan_snap = StateSnapshot(
            state=AgentState.PLAN_WAITING,
            source="push",
            plan_file="/tmp/plan.md",
            plan_title="Add feature X",
            current_issue=99,
        )

        with (
            patch(f"{_REC}.list_sessions", new_callable=AsyncMock, return_value=["ike"]),
            patch(f"{_REC}.get_agent_state", new_callable=AsyncMock, return_value=plan_snap),
            patch(f"{_REC}.check_plan_waiting", new_callable=AsyncMock),
        ):
            await reconcile_startup_states(config, mock_db)

        mock_db.set_agent_state.assert_called_once_with(
            session_name="ike",
            state="plan_waiting",
            current_issue=99,
            entity="ike",
            plan_file="/tmp/plan.md",
            plan_title="Add feature X",
        )

    @pytest.mark.asyncio
    async def test_check_plan_waiting_called(self, config, mock_db):
        """check_plan_waiting is called with active sessions and db."""
        mock_check = AsyncMock()

        with (
            patch(f"{_REC}.list_sessions", new_callable=AsyncMock, return_value=["ike"]),
            patch(
                f"{_REC}.get_agent_state",
                new_callable=AsyncMock,
                return_value=StateSnapshot(state=AgentState.IDLE),
            ),
            patch(f"{_REC}.check_plan_waiting", mock_check),
        ):
            await reconcile_startup_states(config, mock_db)

        mock_check.assert_called_once_with(config, {"ike"}, db=mock_db)

    @pytest.mark.asyncio
    async def test_list_sessions_failure_nonfatal(self, config, mock_db):
        """list_sessions failure is non-fatal — no DB writes, no crash."""
        with patch(
            f"{_REC}.list_sessions",
            new_callable=AsyncMock,
            side_effect=RuntimeError("tmux gone"),
        ):
            await reconcile_startup_states(config, mock_db)

        mock_db.set_agent_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_per_entity_failure_nonfatal(self, config, mock_db):
        """One entity failing doesn't block the other."""
        idle_snap = StateSnapshot(state=AgentState.IDLE, source="push")

        call_count = 0

        async def mock_get_state(state_path, session, stale):
            nonlocal call_count
            call_count += 1
            if session == "ike":
                raise RuntimeError("disk read failed")
            return idle_snap

        with (
            patch(f"{_REC}.list_sessions", new_callable=AsyncMock, return_value=["ike", "feynman"]),
            patch(f"{_REC}.get_agent_state", side_effect=mock_get_state),
            patch(f"{_REC}.check_plan_waiting", new_callable=AsyncMock),
        ):
            await reconcile_startup_states(config, mock_db)

        # ike failed, but feynman should still be synced
        assert mock_db.set_agent_state.call_count == 1
        mock_db.set_agent_state.assert_called_once_with(
            session_name="feynman",
            state="idle",
            current_issue=None,
            entity="feynman",
            plan_file=None,
            plan_title=None,
        )
