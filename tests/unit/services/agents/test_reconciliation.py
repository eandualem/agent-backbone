"""Tests for startup state reconciliation."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.services.agents import AgentState, StateSnapshot
from agent_backbone.services.agents._escalation import _plan_notify_dedup
from agent_backbone.services.agents._reconciliation import reconcile_startup_states
from tests.conftest import make_agents, make_config

_REC = "agent_backbone.services.agents._reconciliation"


@pytest.fixture(autouse=True)
def clear_plan_dedup():
    _plan_notify_dedup.clear()
    yield
    _plan_notify_dedup.clear()


@pytest.fixture
def config(tmp_path):
    return make_config(tmp_path, agents=make_agents(tmp_path, names=("ike", "feynman")))


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.set_agent_state = AsyncMock()
    return db


class TestReconcileStartupStates:
    async def test_no_sessions_early_return(self, config, mock_db):
        with patch(f"{_REC}.list_sessions", new_callable=AsyncMock, return_value=[]):
            await reconcile_startup_states(config, mock_db)
        mock_db.set_agent_state.assert_not_called()

    async def test_syncs_active_agent_states(self, config, mock_db):
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

    async def test_skips_inactive_and_unconfigured_sessions(self, config, mock_db):
        idle_snap = StateSnapshot(state=AgentState.IDLE, source="push")

        with (
            patch(f"{_REC}.list_sessions", new_callable=AsyncMock, return_value=["ike", "random"]),
            patch(f"{_REC}.get_agent_state", new_callable=AsyncMock, return_value=idle_snap),
            patch(f"{_REC}.check_plan_waiting", new_callable=AsyncMock),
        ):
            await reconcile_startup_states(config, mock_db)

        mock_db.set_agent_state.assert_called_once_with(
            session_name="ike",
            state="idle",
            current_issue=None,
            plan_file=None,
            plan_title=None,
            reason=None,
            current_repo=None,
        )

    async def test_plan_waiting_state_synced_with_fields(self, config, mock_db):
        plan_snap = StateSnapshot(
            state=AgentState.WAITING_FOR_HUMAN,
            reason="plan",
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
            state="waiting_for_human",
            current_issue=99,
            plan_file="/tmp/plan.md",
            plan_title="Add feature X",
            reason="plan",
            current_repo=None,
        )

    async def test_check_plan_waiting_called(self, config, mock_db):
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

    async def test_list_sessions_failure_nonfatal(self, config, mock_db):
        with patch(
            f"{_REC}.list_sessions", new_callable=AsyncMock, side_effect=RuntimeError("tmux gone")
        ):
            await reconcile_startup_states(config, mock_db)

        mock_db.set_agent_state.assert_not_called()

    async def test_per_agent_failure_nonfatal(self, config, mock_db):
        idle_snap = StateSnapshot(state=AgentState.IDLE, source="push")

        async def mock_get_state(state_path, session, stale):
            if session == "ike":
                raise RuntimeError("disk read failed")
            return idle_snap

        with (
            patch(f"{_REC}.list_sessions", new_callable=AsyncMock, return_value=["ike", "feynman"]),
            patch(f"{_REC}.get_agent_state", side_effect=mock_get_state),
            patch(f"{_REC}.check_plan_waiting", new_callable=AsyncMock),
        ):
            await reconcile_startup_states(config, mock_db)

        mock_db.set_agent_state.assert_called_once()
        assert mock_db.set_agent_state.call_args.kwargs["session_name"] == "feynman"
