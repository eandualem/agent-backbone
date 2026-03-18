"""Tests for escalation logic (flows/escalation.py) and monitor integration."""

from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from prefect.cache_policies import NO_CACHE

from agent_backbone.config import (
    AgentStateConfig,
    BackboneConfig,
    CapacityRoutingConfig,
    DeliveryConfig,
    EntityConfig,
    EscalationConfig,
    GitHubConfig,
    SchedulingConfig,
    TelegramConfig,
)
from agent_backbone.models import CommentData, IssueData, ParsedLabels
from agent_backbone.services._locator import init as init_flow_services
from agent_backbone.services.agents import (
    AgentState,
    StateSnapshot,
    _escalation_dedup,
    _plan_notify_dedup,
    _should_escalate,
    check_for_stalls,
    check_for_unexpected_offline,
    check_plan_waiting,
    check_pending_issues,
    handle_offline,
    handle_stalls,
    monitor_agents,
)
from agent_backbone.services.registry import EntityEntry, EntityInstance, EntityRegistry, RepoInfo

# Patch target prefixes (keep patch() lines under 100 chars)
_MON = "agent_backbone.services.agents._monitor"
_PEN = "agent_backbone.services.agents._pending"
_ESC = "agent_backbone.services.agents._escalation"


def _make_registry(names: list[str]) -> EntityRegistry:
    """Build a minimal EntityRegistry with the given entity names (session == name)."""
    return EntityRegistry(
        entities={
            n: EntityEntry(session=n, home=f"~/ws/{n}", groups=[], figure="", role="")
            for n in names
        },
        repos=[],
    )


def _make_role_registry() -> EntityRegistry:
    """Registry with two concrete Bell role-instance sessions."""
    return EntityRegistry(
        entities={
            "bell-wf": EntityEntry(
                session="bell-wf",
                home="~/ws/core/code/WF/bell",
                groups=["orchestrators"],
                figure="",
                role="",
                organization="WF",
                entity_type="role-instance",
            ),
            "bell-loveble": EntityEntry(
                session="bell-loveble",
                home="~/ws/core/code/Loveble/bell",
                groups=["orchestrators"],
                figure="",
                role="",
                organization="Loveble",
                entity_type="role-instance",
            ),
        },
        repos=[],
    )


def _make_role_target_registry() -> EntityRegistry:
    """Registry with a role target and org-scoped monitored entities."""
    return EntityRegistry(
        entities={
            "bell": EntityEntry(
                session=None,
                home="~/ws/core/code/WF/bell",
                groups=["orchestrators"],
                figure="",
                role="",
                entity_type="role",
                instances={
                    "wf": EntityInstance(
                        home="~/ws/core/code/WF/bell",
                        session="bell-wf",
                        organization="WF",
                    ),
                    "loveble": EntityInstance(
                        home="~/ws/core/code/Loveble/bell",
                        session="bell-loveble",
                        organization="Loveble",
                    ),
                },
            ),
            "wf-agent": EntityEntry(
                session="wf-agent",
                home="~/ws/core/code/WF/worker",
                groups=[],
                figure="",
                role="",
                organization="WF",
            ),
            "loveble-agent": EntityEntry(
                session="loveble-agent",
                home="~/ws/core/code/Loveble/worker",
                groups=[],
                figure="",
                role="",
                organization="Loveble",
            ),
        },
        repos=[],
    )


@pytest.fixture(autouse=True)
def clear_escalation_dedup():
    """Clear escalation dedup state between tests."""
    _escalation_dedup.clear()
    _plan_notify_dedup.clear()
    yield
    _escalation_dedup.clear()
    _plan_notify_dedup.clear()


@pytest.fixture(autouse=True)
def patch_copy_mode_recovery():
    """Keep monitor tests focused on the behavior under test."""
    with patch(f"{_MON}.handle_copy_mode_recovery", new_callable=AsyncMock) as mock_recovery:
        yield mock_recovery


class TestPrefectTaskConfig:
    def test_offline_and_stall_tasks_disable_prefect_input_caching(self):
        """Prefect must not hash BackboneDB inputs for these monitor tasks."""
        assert check_for_stalls.cache_policy == NO_CACHE
        assert check_for_unexpected_offline.cache_policy == NO_CACHE

    def test_pending_issue_scan_disables_prefect_input_caching(self):
        """Pending issue lookup must not hash GitHub client inputs."""
        assert check_pending_issues.cache_policy == NO_CACHE


@pytest.fixture
def escalation_config():
    """Config with minimal entities for escalation testing."""
    return BackboneConfig(
        webhook_secret="test-secret",
        github=GitHubConfig(owner="eandualem", repo="orchestration"),
        entities=EntityConfig(
            skip=frozenset({"elias"}),
        ),
        registry=_make_registry(["ike", "feynman", "leo"]),
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


class TestShouldEscalate:
    def test_first_escalation_allowed(self):
        assert _should_escalate("feynman", "stall:42", 1800) is True

    def test_duplicate_suppressed(self):
        _should_escalate("feynman", "stall:42", 1800)
        assert _should_escalate("feynman", "stall:42", 1800) is False

    def test_different_session_allowed(self):
        _should_escalate("feynman", "stall:42", 1800)
        assert _should_escalate("leo", "stall:42", 1800) is True

    def test_different_event_key_allowed(self):
        _should_escalate("feynman", "stall:42", 1800)
        assert _should_escalate("feynman", "offline", 1800) is True

    def test_expired_entry_re_allowed(self):
        _should_escalate("feynman", "stall:42", 1800)
        # Simulate expiry by backdating
        key = ("feynman", "stall:42")
        _escalation_dedup[key] = time.monotonic() - 2000
        assert _should_escalate("feynman", "stall:42", 1800) is True


class TestCheckForStalls:
    @pytest.mark.asyncio
    async def test_detects_stall(self, escalation_config):
        stalled_snapshot = StateSnapshot(
            state=AgentState.PROCESSING_ISSUE,
            current_issue=42,
            timestamp=time.time() - 6000,  # 100 minutes, over 90m threshold
            started_at=time.time() - 7200,
            source="push",
        )
        mock_db = AsyncMock()

        with patch(
            f"{_ESC}.get_agent_state",
            new_callable=AsyncMock,
            return_value=stalled_snapshot,
        ):
            stalls = await check_for_stalls.fn(
                escalation_config, {"ike", "feynman", "leo"}, mock_db
            )

        assert len(stalls) >= 1
        stall_entities = [s["entity"] for s in stalls]
        assert "feynman" in stall_entities

    @pytest.mark.asyncio
    async def test_idle_not_stalled(self, escalation_config):
        idle_snapshot = StateSnapshot(
            state=AgentState.IDLE,
            source="pull",
        )
        mock_db = AsyncMock()

        with patch(
            f"{_ESC}.get_agent_state",
            new_callable=AsyncMock,
            return_value=idle_snapshot,
        ):
            stalls = await check_for_stalls.fn(
                escalation_config, {"ike", "feynman", "leo"}, mock_db
            )

        assert len(stalls) == 0

    @pytest.mark.asyncio
    async def test_null_issue_not_stalled(self, escalation_config):
        """Agent with issue=null (housekeeping) should never trigger a stall."""
        busy_no_issue = StateSnapshot(
            state=AgentState.BUSY,
            current_issue=None,
            timestamp=time.time() - 6000,
            started_at=time.time() - 7200,
            source="push",
        )
        mock_db = AsyncMock()

        with patch(
            f"{_ESC}.get_agent_state",
            new_callable=AsyncMock,
            return_value=busy_no_issue,
        ):
            stalls = await check_for_stalls.fn(
                escalation_config, {"ike", "feynman", "leo"}, mock_db
            )

        assert len(stalls) == 0

    @pytest.mark.asyncio
    async def test_recent_timestamp_not_stalled(self, escalation_config):
        """Agent with recent ts (state update) should not stall even with old started_at."""
        recent_ts_snapshot = StateSnapshot(
            state=AgentState.PROCESSING_ISSUE,
            current_issue=42,
            timestamp=time.time() - 60,  # 1 minute ago — well within threshold
            started_at=time.time() - 7200,  # 2 hours — old session
            source="push",
        )
        mock_db = AsyncMock()

        with patch(
            f"{_ESC}.get_agent_state",
            new_callable=AsyncMock,
            return_value=recent_ts_snapshot,
        ):
            stalls = await check_for_stalls.fn(
                escalation_config, {"ike", "feynman", "leo"}, mock_db
            )

        assert len(stalls) == 0

    @pytest.mark.asyncio
    async def test_no_started_at_not_stalled(self, escalation_config):
        busy_no_start = StateSnapshot(
            state=AgentState.BUSY,
            started_at=None,
            source="pull",
        )
        mock_db = AsyncMock()

        with patch(
            f"{_ESC}.get_agent_state",
            new_callable=AsyncMock,
            return_value=busy_no_start,
        ):
            stalls = await check_for_stalls.fn(
                escalation_config, {"ike", "feynman", "leo"}, mock_db
            )

        assert len(stalls) == 0


class TestCheckForUnexpectedOffline:
    @pytest.mark.asyncio
    async def test_detects_offline(self, escalation_config):
        mock_db = AsyncMock()
        mock_db.get_all_agent_states.return_value = [
            {"session_name": "feynman", "state": "idle"},
        ]

        mock_gh = AsyncMock()
        mock_gh.list_open_issues.return_value = [
            IssueData(
                number=1,
                title="[task] Test",
                state="open",
                labels=ParsedLabels(sender="ike", targets=["feynman"]),
            ),
            IssueData(
                number=2,
                title="[task] Test 2",
                state="open",
                labels=ParsedLabels(sender="ike", targets=["feynman"]),
            ),
        ]

        # feynman NOT in active sessions
        offline = await check_for_unexpected_offline.fn(
            escalation_config, {"ike", "leo"}, mock_db, mock_gh
        )

        assert len(offline) == 1
        assert offline[0]["entity"] == "feynman"
        assert offline[0]["pending_count"] == 2

    @pytest.mark.asyncio
    async def test_unknown_state_ignored(self, escalation_config):
        mock_db = AsyncMock()
        mock_db.get_all_agent_states.return_value = [
            {"session_name": "feynman", "state": "unknown"},
        ]

        offline = await check_for_unexpected_offline.fn(
            escalation_config, {"ike", "leo"}, mock_db, AsyncMock()
        )

        assert len(offline) == 0

    @pytest.mark.asyncio
    async def test_active_session_not_flagged(self, escalation_config):
        mock_db = AsyncMock()
        mock_db.get_all_agent_states.return_value = [
            {"session_name": "feynman", "state": "idle"},
        ]

        # feynman IS in active sessions
        offline = await check_for_unexpected_offline.fn(
            escalation_config, {"ike", "feynman", "leo"}, mock_db, AsyncMock()
        )

        assert len(offline) == 0


def _make_monitor_config(**overrides) -> BackboneConfig:
    """Build a minimal config for monitor integration tests."""
    defaults = dict(
        webhook_secret="test",
        entities=EntityConfig(skip=frozenset({"elias"})),
        registry=_make_registry(["ike"]),
        delivery=DeliveryConfig(),
        escalation=EscalationConfig(),
        capacity_routing=CapacityRoutingConfig(),
    )
    defaults.update(overrides)
    return BackboneConfig(**defaults)


class TestMonitorAgentsIntegration:
    @pytest.mark.asyncio
    async def test_defers_busy_agent(self):
        busy_snapshot = StateSnapshot(
            state=AgentState.BUSY,
            started_at=time.time() - 60,
            source="push",
        )
        idle_snapshot = StateSnapshot(
            state=AgentState.IDLE,
            source="pull",
        )
        pending_issue = IssueData(
            number=42,
            title="[task] Test",
            state="open",
            labels=ParsedLabels(sender="leo", targets=["ike"], issue_type="task"),
        )

        call_count = {"get_agent_state": 0}

        async def mock_get_state(state_dir, session, stale_threshold=300.0):
            call_count["get_agent_state"] += 1
            if session == "feynman":
                return busy_snapshot
            return idle_snapshot

        config = _make_monitor_config(registry=_make_registry(["feynman", "ike"]))
        mock_db = AsyncMock()
        mock_db.is_acknowledged.return_value = False
        mock_db.query_deliveries.return_value = []
        mock_gh = AsyncMock()

        init_flow_services(config=config, db=mock_db, gh=mock_gh)

        with (
            patch(
                f"{_MON}.list_sessions",
                new_callable=AsyncMock,
                return_value=["feynman", "ike"],
            ),
            patch(f"{_MON}.sync_dependencies", new_callable=AsyncMock),
            patch(f"{_MON}.handle_stalls", new_callable=AsyncMock),
            patch(f"{_MON}.handle_offline", new_callable=AsyncMock),
            patch(f"{_MON}.check_plan_waiting", new_callable=AsyncMock),
            patch(f"{_PEN}.get_agent_state", side_effect=mock_get_state),
            patch(
                f"{_PEN}.check_pending_issues",
                new_callable=AsyncMock,
                return_value=[pending_issue],
            ),
            patch(
                f"{_PEN}.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ),
            patch(f"{_PEN}.has_commented_on_issue", return_value=False),
        ):
            result = await monitor_agents.fn()

        # Feynman is busy → deferred, Ike is idle → delivered
        assert result["feynman"] == "deferred"
        assert result["ike"] == f"delivered_#{pending_issue.number}"

    @pytest.mark.asyncio
    async def test_monitor_runs_copy_mode_recovery(self, patch_copy_mode_recovery):
        idle_snapshot = StateSnapshot(
            state=AgentState.IDLE,
            source="pull",
        )

        config = _make_monitor_config()
        mock_db = AsyncMock()
        mock_db.is_acknowledged.return_value = False
        mock_db.query_deliveries.return_value = []
        mock_gh = AsyncMock()

        init_flow_services(config=config, db=mock_db, gh=mock_gh)

        with (
            patch(
                f"{_MON}.list_sessions",
                new_callable=AsyncMock,
                return_value=["ike"],
            ),
            patch(f"{_MON}.sync_dependencies", new_callable=AsyncMock),
            patch(f"{_MON}.handle_stalls", new_callable=AsyncMock),
            patch(f"{_MON}.handle_offline", new_callable=AsyncMock),
            patch(f"{_MON}.check_plan_waiting", new_callable=AsyncMock),
            patch(
                f"{_PEN}.get_agent_state",
                new_callable=AsyncMock,
                return_value=idle_snapshot,
            ),
            patch(
                f"{_PEN}.check_pending_issues",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            await monitor_agents.fn()

        patch_copy_mode_recovery.assert_awaited_once_with(config, {"ike"})

    @pytest.mark.asyncio
    async def test_monitor_runs_telemetry_collection(self):
        idle_snapshot = StateSnapshot(
            state=AgentState.IDLE,
            source="pull",
        )

        config = _make_monitor_config()
        mock_db = AsyncMock()
        mock_db.is_acknowledged.return_value = False
        mock_db.query_deliveries.return_value = []
        mock_gh = AsyncMock()

        init_flow_services(config=config, db=mock_db, gh=mock_gh)

        with (
            patch(
                f"{_MON}.list_sessions",
                new_callable=AsyncMock,
                return_value=["ike", "gateway"],
            ),
            patch(f"{_MON}.sync_dependencies", new_callable=AsyncMock),
            patch(f"{_MON}.handle_stalls", new_callable=AsyncMock),
            patch(f"{_MON}.handle_offline", new_callable=AsyncMock),
            patch(f"{_MON}.check_plan_waiting", new_callable=AsyncMock),
            patch(
                f"{_MON}.collect_active_session_telemetry",
                new_callable=AsyncMock,
                return_value={},
            ) as mock_collect,
            patch(
                f"{_PEN}.get_agent_state",
                new_callable=AsyncMock,
                return_value=idle_snapshot,
            ),
            patch(
                f"{_PEN}.check_pending_issues",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            await monitor_agents.fn()

        mock_collect.assert_awaited_once_with(
            config=config,
            db=mock_db,
            active_sessions={"ike", "gateway"},
        )

    @pytest.mark.asyncio
    async def test_monitor_reconciles_sessions_socket_updates(self):
        """Monitor runs the guarded session subscription watcher inside the flow."""
        config = _make_monitor_config()
        mock_db = AsyncMock()
        mock_gh = AsyncMock()

        init_flow_services(config=config, db=mock_db, gh=mock_gh)

        with (
            patch(
                f"{_MON}.list_sessions",
                new_callable=AsyncMock,
                return_value=["ike"],
            ),
            patch(f"{_MON}.sync_dependencies", new_callable=AsyncMock),
            patch(f"{_MON}.handle_stalls", new_callable=AsyncMock),
            patch(f"{_MON}.handle_offline", new_callable=AsyncMock),
            patch(f"{_MON}.check_plan_waiting", new_callable=AsyncMock),
            patch(f"{_MON}.collect_active_session_telemetry", new_callable=AsyncMock),
            patch(f"{_MON}.drain_message_queue", new_callable=AsyncMock),
            patch(f"{_MON}.deliver_pending_issues", new_callable=AsyncMock, return_value={}),
            patch(f"{_MON}.get_sio", return_value=MagicMock()),
            patch(f"{_MON}.emit_sessions_update", new_callable=AsyncMock) as mock_emit,
        ):
            await monitor_agents.fn()

        mock_emit.assert_awaited_once()
        assert mock_emit.await_args.kwargs["only_if_changed"] is True

    @pytest.mark.asyncio
    async def test_monitor_reconciles_lost_swarm_workers(self):
        idle_snapshot = StateSnapshot(
            state=AgentState.IDLE,
            source="pull",
        )

        config = _make_monitor_config()
        mock_db = AsyncMock()
        mock_db.is_acknowledged.return_value = False
        mock_db.query_deliveries.return_value = []
        mock_db.reconcile_swarm_worker_sessions.return_value = 1
        mock_gh = AsyncMock()

        init_flow_services(config=config, db=mock_db, gh=mock_gh)

        with (
            patch(
                f"{_MON}.list_sessions",
                new_callable=AsyncMock,
                side_effect=[["ike"], ["ike", "swarm-24-worker"]],
            ),
            patch(f"{_MON}.sync_dependencies", new_callable=AsyncMock),
            patch(f"{_MON}.handle_stalls", new_callable=AsyncMock),
            patch(f"{_MON}.handle_offline", new_callable=AsyncMock),
            patch(f"{_MON}.check_plan_waiting", new_callable=AsyncMock),
            patch(
                f"{_PEN}.get_agent_state",
                new_callable=AsyncMock,
                return_value=idle_snapshot,
            ),
            patch(
                f"{_PEN}.check_pending_issues",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            await monitor_agents.fn()

        mock_db.reconcile_swarm_worker_sessions.assert_awaited_once_with(
            {"ike", "swarm-24-worker"}
        )

    @pytest.mark.asyncio
    async def test_delivers_to_idle_agent(self):
        idle_snapshot = StateSnapshot(
            state=AgentState.IDLE,
            source="pull",
        )
        pending_issue = IssueData(
            number=10,
            title="[task] Update",
            state="open",
            labels=ParsedLabels(sender="ada", targets=["ike"], issue_type="task"),
        )

        config = _make_monitor_config()
        mock_db = AsyncMock()
        mock_db.is_acknowledged.return_value = False
        mock_db.query_deliveries.return_value = []
        mock_gh = AsyncMock()

        init_flow_services(config=config, db=mock_db, gh=mock_gh)

        with (
            patch(
                f"{_MON}.list_sessions",
                new_callable=AsyncMock,
                return_value=["ike"],
            ),
            patch(f"{_MON}.sync_dependencies", new_callable=AsyncMock),
            patch(f"{_MON}.handle_stalls", new_callable=AsyncMock),
            patch(f"{_MON}.handle_offline", new_callable=AsyncMock),
            patch(f"{_MON}.check_plan_waiting", new_callable=AsyncMock),
            patch(
                f"{_PEN}.get_agent_state",
                new_callable=AsyncMock,
                return_value=idle_snapshot,
            ),
            patch(
                f"{_PEN}.check_pending_issues",
                new_callable=AsyncMock,
                return_value=[pending_issue],
            ),
            patch(
                f"{_PEN}.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
            patch(f"{_PEN}.has_commented_on_issue", return_value=False),
        ):
            result = await monitor_agents.fn()

        assert result["ike"] == "delivered_#10"
        mock_deliver.assert_called_once()

    @pytest.mark.asyncio
    async def test_role_instance_targets_query_concrete_labels(self):
        idle_snapshot = StateSnapshot(
            state=AgentState.IDLE,
            source="pull",
        )
        wf_issue = IssueData(
            number=18,
            title="[task] Bell WF work",
            state="open",
            labels=ParsedLabels(sender="feynman", targets=["bell-wf"], issue_type="task"),
        )
        loveble_issue = IssueData(
            number=19,
            title="[task] Bell Loveble work",
            state="open",
            labels=ParsedLabels(sender="feynman", targets=["bell-loveble"], issue_type="task"),
        )

        config = _make_monitor_config(registry=_make_role_registry())
        mock_db = AsyncMock()
        mock_db.is_acknowledged.return_value = False
        mock_db.query_deliveries.return_value = []
        mock_gh = AsyncMock()
        queried_entities: list[str] = []

        async def mock_check_pending(config, entity, gh):
            queried_entities.append(entity)
            return {
                "bell-wf": [wf_issue],
                "bell-loveble": [loveble_issue],
            }.get(entity, [])

        init_flow_services(config=config, db=mock_db, gh=mock_gh)

        with (
            patch(
                f"{_MON}.list_sessions",
                new_callable=AsyncMock,
                return_value=["bell-wf", "bell-loveble"],
            ),
            patch(f"{_MON}.sync_dependencies", new_callable=AsyncMock),
            patch(f"{_MON}.handle_stalls", new_callable=AsyncMock),
            patch(f"{_MON}.handle_offline", new_callable=AsyncMock),
            patch(f"{_MON}.check_plan_waiting", new_callable=AsyncMock),
            patch(
                f"{_PEN}.get_agent_state",
                new_callable=AsyncMock,
                return_value=idle_snapshot,
            ),
            patch(
                f"{_PEN}.check_pending_issues",
                side_effect=mock_check_pending,
            ),
            patch(
                f"{_PEN}.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
            patch(f"{_PEN}.has_commented_on_issue", return_value=False),
        ):
            result = await monitor_agents.fn()

        assert result["bell-wf"] == "delivered_#18"
        assert result["bell-loveble"] == "delivered_#19"
        assert mock_deliver.await_count == 2
        assert queried_entities == ["bell-wf", "bell-loveble"]

    @pytest.mark.asyncio
    async def test_monitor_skips_recently_delivered(self):
        """Monitor should skip issues that were recently delivered by another flow."""
        from datetime import UTC, datetime

        idle_snapshot = StateSnapshot(
            state=AgentState.IDLE,
            source="pull",
        )
        pending_issue = IssueData(
            number=42,
            title="[task] Test",
            state="open",
            labels=ParsedLabels(sender="leo", targets=["ike"], issue_type="task"),
        )

        # Recent delivery record (just now)
        recent_delivery = {
            "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "flow_name": "issue-dispatcher",
            "outcome": "delivered",
        }

        config = _make_monitor_config(
            scheduling=SchedulingConfig(monitor_interval_seconds=60),
        )
        mock_db = AsyncMock()
        mock_db.is_acknowledged.return_value = False
        mock_db.query_deliveries.return_value = [recent_delivery]
        mock_db.record_delivery = AsyncMock()
        mock_gh = AsyncMock()

        init_flow_services(config=config, db=mock_db, gh=mock_gh)

        with (
            patch(
                f"{_MON}.list_sessions",
                new_callable=AsyncMock,
                return_value=["ike"],
            ),
            patch(f"{_MON}.sync_dependencies", new_callable=AsyncMock),
            patch(f"{_MON}.handle_stalls", new_callable=AsyncMock),
            patch(f"{_MON}.handle_offline", new_callable=AsyncMock),
            patch(f"{_MON}.check_plan_waiting", new_callable=AsyncMock),
            patch(
                f"{_PEN}.get_agent_state",
                new_callable=AsyncMock,
                return_value=idle_snapshot,
            ),
            patch(
                f"{_PEN}.check_pending_issues",
                new_callable=AsyncMock,
                return_value=[pending_issue],
            ),
            patch(
                f"{_PEN}.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
            patch(f"{_PEN}.has_commented_on_issue", return_value=False),
        ):
            result = await monitor_agents.fn()

        assert result["ike"] == "no_deliverable"
        mock_deliver.assert_not_called()

    @pytest.mark.asyncio
    async def test_monitor_skips_acknowledged_issue(self):
        """Monitor should skip issues that the agent has acknowledged."""
        idle_snapshot = StateSnapshot(
            state=AgentState.IDLE,
            source="pull",
        )
        pending_issue = IssueData(
            number=42,
            title="[task] Test",
            state="open",
            labels=ParsedLabels(sender="leo", targets=["ike"], issue_type="task"),
        )

        config = _make_monitor_config()
        mock_db = AsyncMock()
        mock_db.is_acknowledged.return_value = True
        mock_gh = AsyncMock()

        init_flow_services(config=config, db=mock_db, gh=mock_gh)

        with (
            patch(
                f"{_MON}.list_sessions",
                new_callable=AsyncMock,
                return_value=["ike"],
            ),
            patch(f"{_MON}.sync_dependencies", new_callable=AsyncMock),
            patch(f"{_MON}.handle_stalls", new_callable=AsyncMock),
            patch(f"{_MON}.handle_offline", new_callable=AsyncMock),
            patch(f"{_MON}.check_plan_waiting", new_callable=AsyncMock),
            patch(
                f"{_PEN}.get_agent_state",
                new_callable=AsyncMock,
                return_value=idle_snapshot,
            ),
            patch(
                f"{_PEN}.check_pending_issues",
                new_callable=AsyncMock,
                return_value=[pending_issue],
            ),
            patch(
                f"{_PEN}.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
        ):
            result = await monitor_agents.fn()

        assert result["ike"] == "no_deliverable"
        mock_deliver.assert_not_called()

    @pytest.mark.asyncio
    async def test_monitor_records_ack_from_action_log(self):
        """Monitor should record acknowledgment when comment found in action log."""
        idle_snapshot = StateSnapshot(
            state=AgentState.IDLE,
            source="pull",
        )
        pending_issue = IssueData(
            number=42,
            title="[task] Test",
            state="open",
            labels=ParsedLabels(sender="leo", targets=["ike"], issue_type="task"),
        )

        config = _make_monitor_config()
        mock_db = AsyncMock()
        mock_db.is_acknowledged.return_value = False
        mock_gh = AsyncMock()

        init_flow_services(config=config, db=mock_db, gh=mock_gh)

        with (
            patch(
                f"{_MON}.list_sessions",
                new_callable=AsyncMock,
                return_value=["ike"],
            ),
            patch(f"{_MON}.sync_dependencies", new_callable=AsyncMock),
            patch(f"{_MON}.handle_stalls", new_callable=AsyncMock),
            patch(f"{_MON}.handle_offline", new_callable=AsyncMock),
            patch(f"{_MON}.check_plan_waiting", new_callable=AsyncMock),
            patch(
                f"{_PEN}.get_agent_state",
                new_callable=AsyncMock,
                return_value=idle_snapshot,
            ),
            patch(
                f"{_PEN}.check_pending_issues",
                new_callable=AsyncMock,
                return_value=[pending_issue],
            ),
            patch(
                f"{_PEN}.has_commented_on_issue",
                return_value=True,
            ),
            patch(
                f"{_PEN}.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
        ):
            result = await monitor_agents.fn()

        assert result["ike"] == "no_deliverable"
        mock_db.record_acknowledgment.assert_called_once_with(42, "ike")
        mock_deliver.assert_not_called()

    @pytest.mark.asyncio
    async def test_monitor_delivers_second_issue_when_first_acknowledged(self):
        """When first pending issue is acknowledged, monitor should deliver the next one."""
        idle_snapshot = StateSnapshot(
            state=AgentState.IDLE,
            source="pull",
        )
        acked_issue = IssueData(
            number=49,
            title="[task] Already acknowledged",
            state="open",
            labels=ParsedLabels(sender="coding-agent", targets=["ike"], issue_type="task"),
        )
        pending_issue = IssueData(
            number=50,
            title="[question] Waiting for delivery",
            state="open",
            labels=ParsedLabels(sender="coding-agent", targets=["ike"], issue_type="task"),
        )

        config = _make_monitor_config(
            scheduling=SchedulingConfig(monitor_interval_seconds=60),
        )
        mock_db = AsyncMock()
        mock_db.is_acknowledged.side_effect = lambda num, entity: num == 49
        mock_db.query_deliveries.return_value = []
        mock_gh = AsyncMock()

        init_flow_services(config=config, db=mock_db, gh=mock_gh)

        with (
            patch(
                f"{_MON}.list_sessions",
                new_callable=AsyncMock,
                return_value=["ike"],
            ),
            patch(f"{_MON}.sync_dependencies", new_callable=AsyncMock),
            patch(f"{_MON}.handle_stalls", new_callable=AsyncMock),
            patch(f"{_MON}.handle_offline", new_callable=AsyncMock),
            patch(f"{_MON}.check_plan_waiting", new_callable=AsyncMock),
            patch(
                f"{_PEN}.get_agent_state",
                new_callable=AsyncMock,
                return_value=idle_snapshot,
            ),
            patch(
                f"{_PEN}.check_pending_issues",
                new_callable=AsyncMock,
                return_value=[acked_issue, pending_issue],
            ),
            patch(f"{_PEN}.has_commented_on_issue", return_value=False),
            patch(
                f"{_PEN}.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
        ):
            result = await monitor_agents.fn()

        # Should skip #49 (acknowledged) and deliver #50
        assert result["ike"] == "delivered_#50"
        mock_deliver.assert_called_once()


class TestOfflineDedup:
    @pytest.mark.asyncio
    async def test_offline_clears_db_state(self):
        """After notifying about an offline agent, DB state is set to 'unknown'."""
        idle_snapshot = StateSnapshot(
            state=AgentState.IDLE,
            source="pull",
        )

        config = _make_monitor_config(
            registry=_make_registry(["ike", "feynman"]),
            escalation=EscalationConfig(
                escalation_target="ike",
                escalation_dedup_seconds=1800,
            ),
        )
        mock_db = AsyncMock()
        mock_db.is_acknowledged.return_value = False
        mock_db.query_deliveries.return_value = []
        mock_gh = AsyncMock()

        init_flow_services(config=config, db=mock_db, gh=mock_gh)

        with (
            patch(
                f"{_MON}.list_sessions",
                new_callable=AsyncMock,
                return_value=["ike"],  # feynman is NOT active
            ),
            patch(f"{_MON}.sync_dependencies", new_callable=AsyncMock),
            patch(f"{_MON}.handle_stalls", new_callable=AsyncMock),
            patch(
                f"{_ESC}.check_for_unexpected_offline",
                new_callable=AsyncMock,
                return_value=[
                    {"entity": "feynman", "session": "feynman", "pending_count": 0},
                ],
            ),
            patch(f"{_MON}.check_plan_waiting", new_callable=AsyncMock),
            patch(
                f"{_PEN}.get_agent_state",
                new_callable=AsyncMock,
                return_value=idle_snapshot,
            ),
            patch(
                f"{_PEN}.check_pending_issues",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                f"{_ESC}.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
        ):
            await monitor_agents.fn()

        # Escalation message was sent to ike via safe_deliver
        mock_deliver.assert_called()

        # DB state for feynman was cleared to "unknown"
        mock_db.set_agent_state.assert_called_once_with(
            session_name="feynman",
            state="unknown",
            current_issue=None,
        )


class TestRoleEscalationTargetResolution:
    @pytest.mark.asyncio
    async def test_role_escalation_target_wf_stall_notifies_matching_instance(self):
        config = _make_monitor_config(
            registry=_make_role_target_registry(),
            escalation=EscalationConfig(
                escalation_target="bell",
                escalation_dedup_seconds=1800,
            ),
        )
        mock_db = AsyncMock()

        with (
            patch(
                f"{_ESC}.check_for_stalls",
                new_callable=AsyncMock,
                return_value=[
                    {
                        "entity": "wf-agent",
                        "session": "wf-agent",
                        "issue_number": 42,
                        "duration_minutes": 120,
                    }
                ],
            ),
            patch(
                f"{_ESC}.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
        ):
            await handle_stalls(config, {"wf-agent", "bell-wf", "bell-loveble"}, mock_db)

        mock_deliver.assert_awaited_once()
        assert mock_deliver.await_args[0][0] == "bell-wf"

    @pytest.mark.asyncio
    async def test_role_escalation_target_loveble_offline_notifies_matching_instance(self):
        config = _make_monitor_config(
            registry=_make_role_target_registry(),
            escalation=EscalationConfig(
                escalation_target="bell",
                escalation_dedup_seconds=1800,
            ),
        )
        mock_db = AsyncMock()

        with (
            patch(
                f"{_ESC}.check_for_unexpected_offline",
                new_callable=AsyncMock,
                return_value=[
                    {
                        "entity": "loveble-agent",
                        "session": "loveble-agent",
                        "pending_count": 3,
                    }
                ],
            ),
            patch(
                f"{_ESC}.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
        ):
            await handle_offline(config, {"bell-wf", "bell-loveble"}, mock_db, AsyncMock())

        mock_deliver.assert_awaited_once()
        assert mock_deliver.await_args[0][0] == "bell-loveble"
        mock_db.set_agent_state.assert_awaited_once_with(
            session_name="loveble-agent",
            state="unknown",
            current_issue=None,
        )


class TestPlanWaitingMonitor:
    @pytest.mark.asyncio
    async def test_plan_waiting_excluded_from_stalls(self, escalation_config):
        """plan_waiting agents should not be flagged as stalled."""
        plan_snapshot = StateSnapshot(
            state=AgentState.PLAN_WAITING,
            current_issue=None,
            timestamp=time.time() - 6000,
            source="push",
            plan_file="/tmp/plan.md",
            plan_title="Test plan",
        )
        mock_db = AsyncMock()

        with patch(
            f"{_ESC}.get_agent_state",
            new_callable=AsyncMock,
            return_value=plan_snapshot,
        ):
            stalls = await check_for_stalls.fn(
                escalation_config, {"ike", "feynman", "leo"}, mock_db
            )

        assert len(stalls) == 0

    @pytest.mark.asyncio
    async def test_plan_waiting_sends_telegram(self, escalation_config):
        """Monitor should send Telegram notification when agent is plan_waiting."""
        plan_snapshot = StateSnapshot(
            state=AgentState.PLAN_WAITING,
            current_issue=None,
            timestamp=time.time(),
            source="push",
            plan_file="/tmp/plan.md",
            plan_title="Add caching",
        )
        idle_snapshot = StateSnapshot(
            state=AgentState.IDLE,
            source="pull",
        )

        async def mock_get_state(state_dir, session, stale_threshold=300.0):
            if session == "feynman":
                return plan_snapshot
            return idle_snapshot

        config_with_telegram = BackboneConfig(
            webhook_secret="test-secret",
            github=GitHubConfig(owner="eandualem", repo="orchestration"),
            entities=EntityConfig(
                skip=frozenset({"elias"}),
            ),
            registry=_make_registry(["feynman", "ike"]),
            agent_state=AgentStateConfig(
                state_dir="/tmp/test-state",
                stale_threshold_seconds=300,
            ),
            escalation=EscalationConfig(),
            delivery=DeliveryConfig(),
            capacity_routing=CapacityRoutingConfig(),
            telegram=TelegramConfig(notification_chat_id=897573812),
        )

        mock_db = AsyncMock()
        mock_db.is_acknowledged.return_value = False
        mock_db.query_deliveries.return_value = []
        mock_gh = AsyncMock()

        init_flow_services(config=config_with_telegram, db=mock_db, gh=mock_gh)

        with (
            patch(
                f"{_MON}.list_sessions",
                new_callable=AsyncMock,
                return_value=["feynman", "ike"],
            ),
            patch(f"{_MON}.sync_dependencies", new_callable=AsyncMock),
            patch(f"{_MON}.handle_stalls", new_callable=AsyncMock),
            patch(f"{_MON}.handle_offline", new_callable=AsyncMock),
            patch(f"{_ESC}.get_agent_state", side_effect=mock_get_state),
            patch(f"{_PEN}.get_agent_state", side_effect=mock_get_state),
            patch(
                f"{_PEN}.check_pending_issues",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                f"{_PEN}.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ),
            patch.dict(os.environ, {"TELEGRAM_TOKEN": "test-token"}),
            patch(
                f"{_ESC}.TelegramService.send_notification",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_notify,
        ):
            result = await monitor_agents.fn()

        mock_notify.assert_called_once()
        call_args = mock_notify.call_args[0]
        assert call_args[0] == "test-token"
        assert call_args[1] == 897573812
        assert "feynman" in call_args[2].lower() or "feynman" in call_args[2]
        assert "Add caching" in call_args[2]
        # feynman deferred (plan_waiting), ike delivered or no_pending
        assert result.get("feynman") == "deferred"


class TestCodingAgentSweep:
    """Tests for the coding-agent sweep in deliver_pending_issues.

    After processing named entities, the monitor identifies coding-agent
    sessions (active sessions minus named entity sessions minus service
    sessions), validates them against known repo names, fetches
    for:coding-agent issues, and delivers matching issues by repo name
    extracted from the title.
    """

    def _make_coding_config(self) -> BackboneConfig:
        """Build config with one named entity (ike) and one known repo."""
        registry = _make_registry(["ike"])
        registry.add_repo(RepoInfo(org="WF", name="agent-backbone", path="/some/path"))
        return _make_monitor_config(registry=registry)

    @pytest.mark.asyncio
    async def test_coding_agent_sweep_delivers(self):
        """Coding-agent sweep delivers matching issue to idle coding-agent session."""
        idle_snapshot = StateSnapshot(
            state=AgentState.IDLE,
            source="pull",
        )
        coding_issue = IssueData(
            number=100,
            title="[task] agent-backbone: Fix delivery",
            state="open",
            labels=ParsedLabels(sender="bell", targets=["coding-agent"], issue_type="task"),
        )

        async def mock_get_state(state_dir, session, stale_threshold=300.0):
            return idle_snapshot

        async def mock_check_pending(config, entity, gh):
            if entity == "coding-agent":
                return [coding_issue]
            return []

        config = self._make_coding_config()
        mock_db = AsyncMock()
        mock_db.is_acknowledged.return_value = False
        mock_db.query_deliveries.return_value = []
        mock_gh = AsyncMock()

        init_flow_services(config=config, db=mock_db, gh=mock_gh)

        with (
            patch(
                f"{_MON}.list_sessions",
                new_callable=AsyncMock,
                return_value=["ike", "agent-backbone"],
            ),
            patch(f"{_MON}.sync_dependencies", new_callable=AsyncMock),
            patch(f"{_MON}.handle_stalls", new_callable=AsyncMock),
            patch(f"{_MON}.handle_offline", new_callable=AsyncMock),
            patch(f"{_MON}.check_plan_waiting", new_callable=AsyncMock),
            patch(f"{_PEN}.get_agent_state", side_effect=mock_get_state),
            patch(
                f"{_PEN}.check_pending_issues",
                side_effect=mock_check_pending,
            ),
            patch(
                f"{_PEN}.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
            patch(f"{_PEN}.has_commented_on_issue", return_value=False),
        ):
            result = await monitor_agents.fn()

        assert result["coding:agent-backbone"] == "delivered_#100"
        # Verify safe_deliver was called with the coding-agent session
        mock_deliver.assert_called()
        deliver_calls = [c for c in mock_deliver.call_args_list if c[0][0] == "agent-backbone"]
        assert len(deliver_calls) == 1

    @pytest.mark.asyncio
    async def test_coding_agent_sweep_backfills_ack_from_github_comments(self):
        """A GitHub comment from coding-agent should unblock the next queued issue."""
        idle_snapshot = StateSnapshot(
            state=AgentState.IDLE,
            source="pull",
        )
        acked_issue = IssueData(
            number=693,
            title="[task] agent-orchestration-dashboard: Support role-based entity instances",
            state="open",
            labels=ParsedLabels(sender="feynman", targets=["coding-agent"], issue_type="task"),
        )
        next_issue = IssueData(
            number=694,
            title=(
                "[task] agent-orchestration-dashboard: "
                "Align dashboard with role-based entity sessions"
            ),
            state="open",
            labels=ParsedLabels(sender="coding-agent", targets=["coding-agent"], issue_type="task"),
        )

        async def mock_get_state(state_dir, session, stale_threshold=300.0):
            return idle_snapshot

        async def mock_check_pending(config, entity, gh):
            if entity == "coding-agent":
                return [acked_issue, next_issue]
            return []

        config = _make_monitor_config(registry=_make_registry(["ike"]))
        config.registry.add_repo(
            RepoInfo(org="WF", name="agent-orchestration-dashboard", path="/some/path")
        )
        mock_db = AsyncMock()
        mock_db.is_acknowledged.return_value = False
        mock_db.query_deliveries.return_value = []
        mock_gh = AsyncMock()
        mock_gh.list_comments.side_effect = [
            [CommentData(body="[from:coding-agent]\nReviewed.", user_login="eandualem")],
            [],
        ]

        init_flow_services(config=config, db=mock_db, gh=mock_gh)

        with (
            patch(
                f"{_MON}.list_sessions",
                new_callable=AsyncMock,
                return_value=["ike", "agent-orchestration-dashboard"],
            ),
            patch(f"{_MON}.sync_dependencies", new_callable=AsyncMock),
            patch(f"{_MON}.handle_stalls", new_callable=AsyncMock),
            patch(f"{_MON}.handle_offline", new_callable=AsyncMock),
            patch(f"{_MON}.check_plan_waiting", new_callable=AsyncMock),
            patch(f"{_PEN}.get_agent_state", side_effect=mock_get_state),
            patch(
                f"{_PEN}.check_pending_issues",
                side_effect=mock_check_pending,
            ),
            patch(
                f"{_PEN}.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
            patch(f"{_PEN}.has_commented_on_issue", return_value=False),
        ):
            result = await monitor_agents.fn()

        assert result["coding:agent-orchestration-dashboard"] == "delivered_#694"
        mock_db.record_acknowledgment.assert_called_once_with(693, "coding-agent")
        mock_deliver.assert_called_once()

    @pytest.mark.asyncio
    async def test_coding_agent_sweep_skips_busy(self):
        """Coding-agent sweep defers delivery when coding-agent session is busy."""
        idle_snapshot = StateSnapshot(
            state=AgentState.IDLE,
            source="pull",
        )
        busy_snapshot = StateSnapshot(
            state=AgentState.BUSY,
            started_at=time.time() - 60,
            source="push",
        )
        coding_issue = IssueData(
            number=100,
            title="[task] agent-backbone: Fix delivery",
            state="open",
            labels=ParsedLabels(sender="bell", targets=["coding-agent"], issue_type="task"),
        )

        async def mock_get_state(state_dir, session, stale_threshold=300.0):
            if session == "agent-backbone":
                return busy_snapshot
            return idle_snapshot

        async def mock_check_pending(config, entity, gh):
            if entity == "coding-agent":
                return [coding_issue]
            return []

        config = self._make_coding_config()
        mock_db = AsyncMock()
        mock_db.is_acknowledged.return_value = False
        mock_db.query_deliveries.return_value = []
        mock_gh = AsyncMock()

        init_flow_services(config=config, db=mock_db, gh=mock_gh)

        with (
            patch(
                f"{_MON}.list_sessions",
                new_callable=AsyncMock,
                return_value=["ike", "agent-backbone"],
            ),
            patch(f"{_MON}.sync_dependencies", new_callable=AsyncMock),
            patch(f"{_MON}.handle_stalls", new_callable=AsyncMock),
            patch(f"{_MON}.handle_offline", new_callable=AsyncMock),
            patch(f"{_MON}.check_plan_waiting", new_callable=AsyncMock),
            patch(f"{_PEN}.get_agent_state", side_effect=mock_get_state),
            patch(
                f"{_PEN}.check_pending_issues",
                side_effect=mock_check_pending,
            ),
            patch(
                f"{_PEN}.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
            patch(f"{_PEN}.has_commented_on_issue", return_value=False),
        ):
            result = await monitor_agents.fn()

        assert result["coding:agent-backbone"] == "deferred"
        # safe_deliver should NOT have been called for agent-backbone
        deliver_calls = [c for c in mock_deliver.call_args_list if c[0][0] == "agent-backbone"]
        assert len(deliver_calls) == 0

    @pytest.mark.asyncio
    async def test_coding_agent_sweep_skips_no_match(self):
        """Coding-agent sweep reports no_deliverable when no issue title matches the session."""
        idle_snapshot = StateSnapshot(
            state=AgentState.IDLE,
            source="pull",
        )
        # Issue targets a different repo — not agent-backbone
        non_matching_issue = IssueData(
            number=100,
            title="[task] other-repo: Fix something",
            state="open",
            labels=ParsedLabels(sender="bell", targets=["coding-agent"], issue_type="task"),
        )

        async def mock_get_state(state_dir, session, stale_threshold=300.0):
            return idle_snapshot

        async def mock_check_pending(config, entity, gh):
            if entity == "coding-agent":
                return [non_matching_issue]
            return []

        config = self._make_coding_config()
        mock_db = AsyncMock()
        mock_db.is_acknowledged.return_value = False
        mock_db.query_deliveries.return_value = []
        mock_gh = AsyncMock()

        init_flow_services(config=config, db=mock_db, gh=mock_gh)

        with (
            patch(
                f"{_MON}.list_sessions",
                new_callable=AsyncMock,
                return_value=["ike", "agent-backbone"],
            ),
            patch(f"{_MON}.sync_dependencies", new_callable=AsyncMock),
            patch(f"{_MON}.handle_stalls", new_callable=AsyncMock),
            patch(f"{_MON}.handle_offline", new_callable=AsyncMock),
            patch(f"{_MON}.check_plan_waiting", new_callable=AsyncMock),
            patch(f"{_PEN}.get_agent_state", side_effect=mock_get_state),
            patch(
                f"{_PEN}.check_pending_issues",
                side_effect=mock_check_pending,
            ),
            patch(
                f"{_PEN}.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
            patch(f"{_PEN}.has_commented_on_issue", return_value=False),
        ):
            result = await monitor_agents.fn()

        assert result["coding:agent-backbone"] == "no_deliverable"
        # safe_deliver should NOT have been called for agent-backbone
        deliver_calls = [c for c in mock_deliver.call_args_list if c[0][0] == "agent-backbone"]
        assert len(deliver_calls) == 0

    @pytest.mark.asyncio
    async def test_repo_local_issue_sweep_delivers(self):
        """Repo-local open issues are delivered to the matching repo session."""
        idle_snapshot = StateSnapshot(
            state=AgentState.IDLE,
            source="pull",
        )
        repo_issue = IssueData(
            number=201,
            title="Fix webhook repo fallback",
            state="open",
            labels=ParsedLabels(sender="unknown", targets=[], issue_type="task"),
            repo_full_name="eandualem/agent-backbone",
        )

        async def mock_get_state(state_dir, session, stale_threshold=300.0):
            return idle_snapshot

        async def mock_check_pending(config, entity, gh):
            return []

        config = self._make_coding_config()
        mock_db = AsyncMock()
        mock_db.is_acknowledged.return_value = False
        mock_db.query_deliveries.return_value = []
        mock_gh = AsyncMock()
        mock_gh.list_issues = AsyncMock(return_value=[repo_issue])

        init_flow_services(config=config, db=mock_db, gh=mock_gh)

        with (
            patch(
                f"{_MON}.list_sessions",
                new_callable=AsyncMock,
                return_value=["ike", "agent-backbone"],
            ),
            patch(f"{_MON}.sync_dependencies", new_callable=AsyncMock),
            patch(f"{_MON}.handle_stalls", new_callable=AsyncMock),
            patch(f"{_MON}.handle_offline", new_callable=AsyncMock),
            patch(f"{_MON}.check_plan_waiting", new_callable=AsyncMock),
            patch(f"{_PEN}.get_agent_state", side_effect=mock_get_state),
            patch(
                f"{_PEN}.check_pending_issues",
                side_effect=mock_check_pending,
            ),
            patch(
                f"{_PEN}.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
            patch(f"{_PEN}.has_commented_on_issue", return_value=False),
        ):
            result = await monitor_agents.fn()

        assert result["repo:agent-backbone"] == "delivered_#201"
        mock_gh.list_issues.assert_awaited_once_with(
            state="open",
            repo_full_name="eandualem/agent-backbone",
        )
        deliver_calls = [c for c in mock_deliver.call_args_list if c[0][0] == "agent-backbone"]
        assert len(deliver_calls) == 1


def _make_orch_registry() -> EntityRegistry:
    """Build registry with orchestrator entities and a WF repo."""
    return EntityRegistry(
        entities={
            "bell": EntityEntry(
                session="bell",
                home="~/ws/core/bell/",
                groups=["orchestrators"],
                figure="",
                role="",
                organization="WF",
            ),
            "ike": EntityEntry(
                session="ike",
                home="~/ws/core/ike/",
                groups=["orchestrators"],
                figure="",
                role="",
                organization="",
            ),
        },
        repos=[RepoInfo(org="WF", name="agent-backbone", path="/ws/core/code/WF/agent-backbone")],
    )


class TestPlanOrchestratorNotification:
    """Tests for orchestrator tmux notification on plan_waiting."""

    @pytest.mark.asyncio
    async def test_plan_waiting_notifies_orchestrator(self):
        """Coding agent in plan_waiting -- orchestrator gets tmux notification."""
        plan_snapshot = StateSnapshot(
            state=AgentState.PLAN_WAITING,
            current_issue=609,
            timestamp=time.time(),
            source="push",
            plan_file="/tmp/plans/plan.md",
            plan_title="Add orchestrator notification",
        )

        registry = EntityRegistry(
            entities={
                "bell": EntityEntry(
                    session="bell",
                    home="~/ws/core/bell/",
                    groups=["orchestrators"],
                    figure="",
                    role="",
                    organization="WF",
                ),
                "ike": EntityEntry(
                    session="ike",
                    home="~/ws/core/ike/",
                    groups=["orchestrators"],
                    figure="",
                    role="",
                    organization="",
                ),
                "backbone-agent": EntityEntry(
                    session="agent-backbone",
                    home="~/ws/core/code/WF/agent-backbone/",
                    groups=[],
                    figure="",
                    role="",
                ),
            },
            repos=[
                RepoInfo(org="WF", name="agent-backbone", path="/ws/core/code/WF/agent-backbone"),
            ],
        )
        config = BackboneConfig(
            webhook_secret="test-secret",
            github=GitHubConfig(owner="eandualem", repo="orchestration"),
            entities=EntityConfig(skip=frozenset({"elias"})),
            registry=registry,
            agent_state=AgentStateConfig(
                state_dir="/tmp/test-state",
                stale_threshold_seconds=300,
            ),
            escalation=EscalationConfig(escalation_target="ike"),
            delivery=DeliveryConfig(),
            capacity_routing=CapacityRoutingConfig(),
            telegram=TelegramConfig(notification_chat_id=0),
        )

        idle_snapshot = StateSnapshot(state=AgentState.IDLE, source="pull")

        async def mock_get_state(state_dir, session, stale_threshold=300.0):
            if session == "agent-backbone":
                return plan_snapshot
            return idle_snapshot

        mock_db = AsyncMock()

        with (
            patch(f"{_ESC}.get_agent_state", side_effect=mock_get_state),
            patch(
                f"{_ESC}.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
        ):
            await check_plan_waiting(config, {"agent-backbone", "bell", "ike"}, db=mock_db)

        # safe_deliver should have been called with bell's session
        mock_deliver.assert_called_once()
        call_args = mock_deliver.call_args
        assert call_args[0][0] == "bell"  # target session
        assert "agent-backbone" in call_args[0][1]  # message mentions session
        assert "Add orchestrator notification" in call_args[0][1]  # plan title
        assert "issue #609" in call_args[0][1]  # issue number
        assert call_args[1]["priority"] is True

    @pytest.mark.asyncio
    async def test_plan_waiting_role_orchestrator_notifies_matching_instance(self):
        """Coding agent in plan_waiting notifies the org-specific role instance."""
        plan_snapshot = StateSnapshot(
            state=AgentState.PLAN_WAITING,
            current_issue=609,
            timestamp=time.time(),
            source="push",
            plan_file="/tmp/plans/plan.md",
            plan_title="Role orchestrator notification",
        )

        registry = EntityRegistry(
            entities={
                "bell": EntityEntry(
                    session=None,
                    home="~/ws/core/code/WF/bell/",
                    groups=["orchestrators"],
                    figure="",
                    role="",
                    entity_type="role",
                    instances={
                        "wf": EntityInstance(
                            home="~/ws/core/code/WF/bell/",
                            session="bell-wf",
                            organization="WF",
                        ),
                        "loveble": EntityInstance(
                            home="~/ws/core/code/Loveble/bell/",
                            session="bell-loveble",
                            organization="Loveble",
                        ),
                    },
                ),
                "backbone-agent": EntityEntry(
                    session="agent-backbone",
                    home="~/ws/core/code/WF/agent-backbone/",
                    groups=[],
                    figure="",
                    role="",
                ),
            },
            repos=[
                RepoInfo(org="WF", name="agent-backbone", path="/ws/core/code/WF/agent-backbone"),
            ],
        )
        config = BackboneConfig(
            webhook_secret="test-secret",
            github=GitHubConfig(owner="eandualem", repo="orchestration"),
            entities=EntityConfig(skip=frozenset({"elias"})),
            registry=registry,
            agent_state=AgentStateConfig(
                state_dir="/tmp/test-state",
                stale_threshold_seconds=300,
            ),
            escalation=EscalationConfig(escalation_target="ike"),
            delivery=DeliveryConfig(),
            capacity_routing=CapacityRoutingConfig(),
            telegram=TelegramConfig(notification_chat_id=0),
        )

        async def mock_get_state(state_dir, session, stale_threshold=300.0):
            if session == "agent-backbone":
                return plan_snapshot
            return StateSnapshot(state=AgentState.IDLE, source="pull")

        mock_db = AsyncMock()

        with (
            patch(f"{_ESC}.get_agent_state", side_effect=mock_get_state),
            patch(
                f"{_ESC}.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
        ):
            await check_plan_waiting(
                config,
                {"agent-backbone", "bell-wf", "bell-loveble"},
                db=mock_db,
            )

        mock_deliver.assert_called_once()
        call_args = mock_deliver.call_args
        assert call_args[0][0] == "bell-wf"
        assert "Role orchestrator notification" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_plan_waiting_orchestrator_dedup(self):
        """Second call for same plan doesn't re-notify orchestrator."""
        plan_snapshot = StateSnapshot(
            state=AgentState.PLAN_WAITING,
            current_issue=None,
            timestamp=time.time(),
            source="push",
            plan_file="/tmp/plans/plan.md",
            plan_title="Test plan",
        )

        registry = EntityRegistry(
            entities={
                "bell": EntityEntry(
                    session="bell",
                    home="~/ws/core/bell/",
                    groups=["orchestrators"],
                    figure="",
                    role="",
                    organization="WF",
                ),
                "backbone-agent": EntityEntry(
                    session="agent-backbone",
                    home="~/ws/core/code/WF/agent-backbone/",
                    groups=[],
                    figure="",
                    role="",
                ),
            },
            repos=[
                RepoInfo(org="WF", name="agent-backbone", path="/ws/core/code/WF/agent-backbone"),
            ],
        )
        config = BackboneConfig(
            webhook_secret="test-secret",
            github=GitHubConfig(owner="eandualem", repo="orchestration"),
            entities=EntityConfig(skip=frozenset({"elias"})),
            registry=registry,
            agent_state=AgentStateConfig(
                state_dir="/tmp/test-state",
                stale_threshold_seconds=300,
            ),
            escalation=EscalationConfig(escalation_target="ike"),
            delivery=DeliveryConfig(),
            capacity_routing=CapacityRoutingConfig(),
            telegram=TelegramConfig(notification_chat_id=0),
        )

        async def mock_get_state(state_dir, session, stale_threshold=300.0):
            if session == "agent-backbone":
                return plan_snapshot
            return StateSnapshot(state=AgentState.IDLE, source="pull")

        with (
            patch(f"{_ESC}.get_agent_state", side_effect=mock_get_state),
            patch(
                f"{_ESC}.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
        ):
            # First call
            await check_plan_waiting(config, {"agent-backbone", "bell"})
            # Second call
            await check_plan_waiting(config, {"agent-backbone", "bell"})

        # Should only deliver once
        assert mock_deliver.call_count == 1

    @pytest.mark.asyncio
    async def test_plan_waiting_orchestrator_dedup_survives_memory_reset(self):
        """Persisted dedup prevents redelivery after process-local cache reset."""
        plan_snapshot = StateSnapshot(
            state=AgentState.PLAN_WAITING,
            current_issue=None,
            timestamp=time.time(),
            source="push",
            plan_file="/tmp/plans/plan.md",
            plan_title="Test plan",
        )

        registry = EntityRegistry(
            entities={
                "bell": EntityEntry(
                    session="bell",
                    home="~/ws/core/bell/",
                    groups=["orchestrators"],
                    figure="",
                    role="",
                    organization="WF",
                ),
                "backbone-agent": EntityEntry(
                    session="agent-backbone",
                    home="~/ws/core/code/WF/agent-backbone/",
                    groups=[],
                    figure="",
                    role="",
                ),
            },
            repos=[
                RepoInfo(org="WF", name="agent-backbone", path="/ws/core/code/WF/agent-backbone"),
            ],
        )
        config = BackboneConfig(
            webhook_secret="test-secret",
            github=GitHubConfig(owner="eandualem", repo="orchestration"),
            entities=EntityConfig(skip=frozenset({"elias"})),
            registry=registry,
            agent_state=AgentStateConfig(
                state_dir="/tmp/test-state",
                stale_threshold_seconds=300,
            ),
            escalation=EscalationConfig(escalation_target="ike"),
            delivery=DeliveryConfig(),
            capacity_routing=CapacityRoutingConfig(),
            telegram=TelegramConfig(notification_chat_id=0),
        )

        async def mock_get_state(state_dir, session, stale_threshold=300.0):
            if session == "agent-backbone":
                return plan_snapshot
            return StateSnapshot(state=AgentState.IDLE, source="pull")

        mock_db = AsyncMock()
        mock_db.has_activity_event = AsyncMock(side_effect=[False, True])

        with (
            patch(f"{_ESC}.get_agent_state", side_effect=mock_get_state),
            patch(
                f"{_ESC}.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
        ):
            await check_plan_waiting(config, {"agent-backbone", "bell"}, db=mock_db)
            _plan_notify_dedup.clear()
            await check_plan_waiting(config, {"agent-backbone", "bell"}, db=mock_db)

        assert mock_deliver.call_count == 1
        assert mock_db.record_activity.await_count == 1

    @pytest.mark.asyncio
    async def test_plan_waiting_same_path_new_timestamp_renotifies(self):
        """A fresh plan with the same path still triggers a new notification."""
        first_snapshot = StateSnapshot(
            state=AgentState.PLAN_WAITING,
            current_issue=None,
            timestamp=time.time(),
            source="push",
            plan_file="/tmp/plans/plan.md",
            plan_title="Test plan",
        )
        second_snapshot = StateSnapshot(
            state=AgentState.PLAN_WAITING,
            current_issue=None,
            timestamp=first_snapshot.timestamp + 30,
            source="push",
            plan_file="/tmp/plans/plan.md",
            plan_title="Test plan",
        )

        registry = EntityRegistry(
            entities={
                "bell": EntityEntry(
                    session="bell",
                    home="~/ws/core/bell/",
                    groups=["orchestrators"],
                    figure="",
                    role="",
                    organization="WF",
                ),
                "backbone-agent": EntityEntry(
                    session="agent-backbone",
                    home="~/ws/core/code/WF/agent-backbone/",
                    groups=[],
                    figure="",
                    role="",
                ),
            },
            repos=[
                RepoInfo(org="WF", name="agent-backbone", path="/ws/core/code/WF/agent-backbone"),
            ],
        )
        config = BackboneConfig(
            webhook_secret="test-secret",
            github=GitHubConfig(owner="eandualem", repo="orchestration"),
            entities=EntityConfig(skip=frozenset({"elias"})),
            registry=registry,
            agent_state=AgentStateConfig(
                state_dir="/tmp/test-state",
                stale_threshold_seconds=300,
            ),
            escalation=EscalationConfig(escalation_target="ike"),
            delivery=DeliveryConfig(),
            capacity_routing=CapacityRoutingConfig(),
            telegram=TelegramConfig(notification_chat_id=0),
        )

        snapshots = [first_snapshot, second_snapshot]

        async def mock_get_state(state_dir, session, stale_threshold=300.0):
            if session == "agent-backbone":
                return snapshots.pop(0)
            return StateSnapshot(state=AgentState.IDLE, source="pull")

        mock_db = AsyncMock()
        mock_db.has_activity_event = AsyncMock(return_value=False)

        with (
            patch(f"{_ESC}.get_agent_state", side_effect=mock_get_state),
            patch(
                f"{_ESC}.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
        ):
            await check_plan_waiting(config, {"agent-backbone", "bell"}, db=mock_db)
            await check_plan_waiting(config, {"agent-backbone", "bell"}, db=mock_db)

        assert mock_deliver.call_count == 2

    @pytest.mark.asyncio
    async def test_plan_waiting_orchestrator_offline(self):
        """Orchestrator not in active sessions -- no delivery attempt."""
        plan_snapshot = StateSnapshot(
            state=AgentState.PLAN_WAITING,
            current_issue=None,
            timestamp=time.time(),
            source="push",
            plan_file="/tmp/plans/plan.md",
            plan_title="Test plan",
        )

        registry = EntityRegistry(
            entities={
                "bell": EntityEntry(
                    session="bell",
                    home="~/ws/core/bell/",
                    groups=["orchestrators"],
                    figure="",
                    role="",
                    organization="WF",
                ),
                "backbone-agent": EntityEntry(
                    session="agent-backbone",
                    home="~/ws/core/code/WF/agent-backbone/",
                    groups=[],
                    figure="",
                    role="",
                ),
            },
            repos=[
                RepoInfo(org="WF", name="agent-backbone", path="/ws/core/code/WF/agent-backbone"),
            ],
        )
        config = BackboneConfig(
            webhook_secret="test-secret",
            github=GitHubConfig(owner="eandualem", repo="orchestration"),
            entities=EntityConfig(skip=frozenset({"elias"})),
            registry=registry,
            agent_state=AgentStateConfig(
                state_dir="/tmp/test-state",
                stale_threshold_seconds=300,
            ),
            escalation=EscalationConfig(escalation_target="ike"),
            delivery=DeliveryConfig(),
            capacity_routing=CapacityRoutingConfig(),
            telegram=TelegramConfig(notification_chat_id=0),
        )

        async def mock_get_state(state_dir, session, stale_threshold=300.0):
            if session == "agent-backbone":
                return plan_snapshot
            return StateSnapshot(state=AgentState.IDLE, source="pull")

        with (
            patch(f"{_ESC}.get_agent_state", side_effect=mock_get_state),
            patch(
                f"{_ESC}.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
        ):
            # bell is NOT in active_sessions
            await check_plan_waiting(config, {"agent-backbone"})

        mock_deliver.assert_not_called()

    @pytest.mark.asyncio
    async def test_plan_waiting_named_entity_falls_back_to_escalation_target(self):
        """Named entity (not a repo) falls back to escalation_target for orchestrator."""
        plan_snapshot = StateSnapshot(
            state=AgentState.PLAN_WAITING,
            current_issue=None,
            timestamp=time.time(),
            source="push",
            plan_file="/tmp/plans/plan.md",
            plan_title="Test plan",
        )

        registry = EntityRegistry(
            entities={
                "feynman": EntityEntry(
                    session="feynman",
                    home="~/ws/feynman/",
                    groups=["optimization"],
                    figure="",
                    role="",
                ),
                "ike": EntityEntry(
                    session="ike",
                    home="~/ws/core/ike/",
                    groups=["orchestrators"],
                    figure="",
                    role="",
                    organization="",
                ),
            },
            repos=[],
        )
        config = BackboneConfig(
            webhook_secret="test-secret",
            github=GitHubConfig(owner="eandualem", repo="orchestration"),
            entities=EntityConfig(skip=frozenset({"elias"})),
            registry=registry,
            agent_state=AgentStateConfig(
                state_dir="/tmp/test-state",
                stale_threshold_seconds=300,
            ),
            escalation=EscalationConfig(escalation_target="ike"),
            delivery=DeliveryConfig(),
            capacity_routing=CapacityRoutingConfig(),
            telegram=TelegramConfig(notification_chat_id=0),
        )

        idle_snapshot = StateSnapshot(state=AgentState.IDLE, source="pull")

        async def mock_get_state(state_dir, session, stale_threshold=300.0):
            if session == "feynman":
                return plan_snapshot
            return idle_snapshot

        with (
            patch(f"{_ESC}.get_agent_state", side_effect=mock_get_state),
            patch(
                f"{_ESC}.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
        ):
            await check_plan_waiting(config, {"feynman", "ike"})

        # Should deliver to ike (escalation target)
        mock_deliver.assert_called_once()
        assert mock_deliver.call_args[0][0] == "ike"

    @pytest.mark.asyncio
    async def test_stale_plan_waiting_without_plan_file_does_not_notify_escalation_target(
        self, tmp_path
    ):
        """A dead plan_waiting file must not page Ike for named entities."""
        state_file = tmp_path / "feynman.json"
        state_file.write_text(
            '{"state":"plan_waiting","ts":1.0,"plan_file":"%s","plan_title":"Add caching"}'
            % (tmp_path / "missing-plan.md")
        )

        registry = EntityRegistry(
            entities={
                "feynman": EntityEntry(
                    session="feynman",
                    home="~/ws/feynman/",
                    groups=["optimization"],
                    figure="",
                    role="",
                ),
                "ike": EntityEntry(
                    session="ike",
                    home="~/ws/core/ike/",
                    groups=["orchestrators"],
                    figure="",
                    role="",
                    organization="",
                ),
            },
            repos=[],
        )
        config = BackboneConfig(
            webhook_secret="test-secret",
            github=GitHubConfig(owner="eandualem", repo="orchestration"),
            entities=EntityConfig(skip=frozenset({"elias"})),
            registry=registry,
            agent_state=AgentStateConfig(
                state_dir=str(tmp_path),
                stale_threshold_seconds=300,
            ),
            escalation=EscalationConfig(escalation_target="ike"),
            delivery=DeliveryConfig(),
            capacity_routing=CapacityRoutingConfig(),
            telegram=TelegramConfig(notification_chat_id=0),
        )

        with (
            patch(
                "agent_backbone.services.agents._inference.capture_pane",
                new_callable=AsyncMock,
                return_value="random output",
            ),
            patch(
                f"{_ESC}.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
        ):
            await check_plan_waiting(config, {"feynman", "ike"})

        mock_deliver.assert_not_called()
