"""Tests for startup state seeding."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call, patch

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
from agent_backbone.services.agents import AgentState, StateSnapshot
from agent_backbone.services.agents._startup import seed_startup_states
from agent_backbone.services.registry import EntityEntry, EntityRegistry, RepoInfo

_STARTUP = "agent_backbone.services.agents._startup"


def _make_registry(names: list[str]) -> EntityRegistry:
    return EntityRegistry(
        entities={
            n: EntityEntry(session=n, home=f"~/ws/{n}", groups=[], figure="", role="")
            for n in names
        },
        repos=[RepoInfo(org="WF", name="agent-backbone", path="/tmp/agent-backbone")],
    )


@pytest.fixture
def config():
    return BackboneConfig(
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


class TestSeedStartupStates:
    @pytest.mark.asyncio
    async def test_no_sessions_early_return(self, config, mock_db):
        with patch(f"{_STARTUP}.list_sessions", new_callable=AsyncMock, return_value=[]):
            await seed_startup_states(config, mock_db)
        mock_db.set_agent_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_list_sessions_failure_nonfatal(self, config, mock_db):
        with patch(
            f"{_STARTUP}.list_sessions",
            new_callable=AsyncMock,
            side_effect=RuntimeError("tmux gone"),
        ):
            await seed_startup_states(config, mock_db)

        mock_db.set_agent_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_seeds_active_entity_and_repo_sessions(self, config, mock_db):
        observations = {
            "agent-backbone": MagicMock(session="agent-backbone"),
            "ike": MagicMock(session="ike"),
        }
        snapshots = {
            "agent-backbone": StateSnapshot(
                state=AgentState.SUB_AGENT_WAITING,
                source="observed",
                timestamp=123.0,
            ),
            "ike": StateSnapshot(state=AgentState.IDLE, source="observed", timestamp=456.0),
        }

        async def mock_observe(session_name: str):
            return observations[session_name]

        def mock_snapshot(observation):
            return snapshots[observation.session]

        with (
            patch(
                f"{_STARTUP}.list_sessions",
                new_callable=AsyncMock,
                return_value=["ike", "agent-backbone", "gateway", "random-shell"],
            ),
            patch(f"{_STARTUP}.observe_session", side_effect=mock_observe),
            patch(f"{_STARTUP}.snapshot_from_observation", side_effect=mock_snapshot),
        ):
            await seed_startup_states(config, mock_db)

        assert mock_db.set_agent_state.await_count == 2
        mock_db.set_agent_state.assert_has_awaits(
            [
                call(
                    session_name="agent-backbone",
                    state="sub_agent_waiting",
                    current_issue=None,
                    entity="agent-backbone",
                    context=None,
                    ts="123.0",
                    plan_file=None,
                    plan_title=None,
                ),
                call(
                    session_name="ike",
                    state="idle",
                    current_issue=None,
                    entity="ike",
                    context=None,
                    ts="456.0",
                    plan_file=None,
                    plan_title=None,
                ),
            ],
            any_order=True,
        )

    @pytest.mark.asyncio
    async def test_per_session_failure_nonfatal(self, config, mock_db):
        observations = {
            "ike": MagicMock(session="ike"),
            "feynman": MagicMock(session="feynman"),
        }

        async def mock_observe(session_name: str):
            if session_name == "ike":
                raise RuntimeError("pgrep failed")
            return observations[session_name]

        with (
            patch(
                f"{_STARTUP}.list_sessions",
                new_callable=AsyncMock,
                return_value=["ike", "feynman"],
            ),
            patch(f"{_STARTUP}.observe_session", side_effect=mock_observe),
            patch(
                f"{_STARTUP}.snapshot_from_observation",
                return_value=StateSnapshot(state=AgentState.IDLE, source="observed", timestamp=1.0),
            ),
        ):
            await seed_startup_states(config, mock_db)

        mock_db.set_agent_state.assert_awaited_once_with(
            session_name="feynman",
            state="idle",
            current_issue=None,
            entity="feynman",
            context=None,
            ts="1.0",
            plan_file=None,
            plan_title=None,
        )
