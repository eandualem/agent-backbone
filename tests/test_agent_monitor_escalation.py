"""Tests for escalation logic in flows/agent_monitor.py."""

from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, patch

import pytest

from flows.agent_monitor import (
    _escalation_dedup,
    _plan_notify_dedup,
    _should_escalate,
    check_for_stalls,
    check_for_unexpected_offline,
    monitor_agents,
)
from src.agent_state import AgentState, StateSnapshot
from src.config import (
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
from src.models import IssueData, ParsedLabels
from src.registry import EntityEntry, EntityRegistry


def _make_registry(names: list[str]) -> EntityRegistry:
    """Build a minimal EntityRegistry with the given entity names (session == name)."""
    return EntityRegistry(
        entities={
            n: EntityEntry(session=n, home=f"~/ws/{n}", groups=[], figure="", role="")
            for n in names
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


@pytest.fixture
def escalation_config():
    """Config with minimal entities for escalation testing."""
    return BackboneConfig(
        github_token="test-token",
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
        delivery=DeliveryConfig(db_path=":memory:"),
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
        with (
            patch(
                "flows.agent_monitor.get_agent_state",
                new_callable=AsyncMock,
                return_value=stalled_snapshot,
            ),
            patch("flows.agent_monitor.BackboneDB") as mock_db_cls,
        ):
            mock_db = AsyncMock()
            mock_db_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_db_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            stalls = await check_for_stalls.fn(escalation_config, {"ike", "feynman", "leo"})

        assert len(stalls) >= 1
        stall_entities = [s["entity"] for s in stalls]
        # All entities are stalled with the same mock
        assert "feynman" in stall_entities

    @pytest.mark.asyncio
    async def test_idle_not_stalled(self, escalation_config):
        idle_snapshot = StateSnapshot(
            state=AgentState.IDLE,
            source="pull",
        )
        with (
            patch(
                "flows.agent_monitor.get_agent_state",
                new_callable=AsyncMock,
                return_value=idle_snapshot,
            ),
            patch("flows.agent_monitor.BackboneDB") as mock_db_cls,
        ):
            mock_db = AsyncMock()
            mock_db_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_db_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            stalls = await check_for_stalls.fn(escalation_config, {"ike", "feynman", "leo"})

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
        with (
            patch(
                "flows.agent_monitor.get_agent_state",
                new_callable=AsyncMock,
                return_value=busy_no_issue,
            ),
            patch("flows.agent_monitor.BackboneDB") as mock_db_cls,
        ):
            mock_db = AsyncMock()
            mock_db_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_db_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            stalls = await check_for_stalls.fn(escalation_config, {"ike", "feynman", "leo"})

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
        with (
            patch(
                "flows.agent_monitor.get_agent_state",
                new_callable=AsyncMock,
                return_value=recent_ts_snapshot,
            ),
            patch("flows.agent_monitor.BackboneDB") as mock_db_cls,
        ):
            mock_db = AsyncMock()
            mock_db_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_db_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            stalls = await check_for_stalls.fn(escalation_config, {"ike", "feynman", "leo"})

        assert len(stalls) == 0

    @pytest.mark.asyncio
    async def test_no_started_at_not_stalled(self, escalation_config):
        busy_no_start = StateSnapshot(
            state=AgentState.BUSY,
            started_at=None,
            source="pull",
        )
        with (
            patch(
                "flows.agent_monitor.get_agent_state",
                new_callable=AsyncMock,
                return_value=busy_no_start,
            ),
            patch("flows.agent_monitor.BackboneDB") as mock_db_cls,
        ):
            mock_db = AsyncMock()
            mock_db_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_db_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            stalls = await check_for_stalls.fn(escalation_config, {"ike", "feynman", "leo"})

        assert len(stalls) == 0


class TestCheckForUnexpectedOffline:
    @pytest.mark.asyncio
    async def test_detects_offline(self, escalation_config):
        with (
            patch("flows.agent_monitor.BackboneDB") as mock_db_cls,
            patch("flows.agent_monitor.GitHubClient") as mock_gh_cls,
        ):
            mock_db = AsyncMock()
            mock_db.get_all_agent_states.return_value = [
                {"session_name": "feynman", "state": "idle"},
            ]
            mock_db_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_db_cls.return_value.__aexit__ = AsyncMock(return_value=False)

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
            mock_gh_cls.return_value.__aenter__ = AsyncMock(return_value=mock_gh)
            mock_gh_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            # feynman NOT in active sessions
            offline = await check_for_unexpected_offline.fn(escalation_config, {"ike", "leo"})

        assert len(offline) == 1
        assert offline[0]["entity"] == "feynman"
        assert offline[0]["pending_count"] == 2

    @pytest.mark.asyncio
    async def test_unknown_state_ignored(self, escalation_config):
        with patch("flows.agent_monitor.BackboneDB") as mock_db_cls:
            mock_db = AsyncMock()
            mock_db.get_all_agent_states.return_value = [
                {"session_name": "feynman", "state": "unknown"},
            ]
            mock_db_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_db_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            offline = await check_for_unexpected_offline.fn(escalation_config, {"ike", "leo"})

        assert len(offline) == 0

    @pytest.mark.asyncio
    async def test_active_session_not_flagged(self, escalation_config):
        with patch("flows.agent_monitor.BackboneDB") as mock_db_cls:
            mock_db = AsyncMock()
            mock_db.get_all_agent_states.return_value = [
                {"session_name": "feynman", "state": "idle"},
            ]
            mock_db_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_db_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            # feynman IS in active sessions
            offline = await check_for_unexpected_offline.fn(
                escalation_config, {"ike", "feynman", "leo"}
            )

        assert len(offline) == 0


class TestMonitorAgentsIntegration:
    @pytest.mark.asyncio
    async def test_defers_busy_agent(self):
        busy_snapshot = StateSnapshot(
            state=AgentState.BUSY,
            started_at=time.time() - 60,  # 1 minute, under threshold
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

        with (
            patch(
                "flows.agent_monitor.BackboneConfig.from_toml",
                return_value=BackboneConfig(
                    github_token="test",
                    webhook_secret="test",
                    entities=EntityConfig(
                        skip=frozenset({"elias"}),
                    ),
                    registry=_make_registry(["feynman", "ike"]),
                    delivery=DeliveryConfig(db_path=":memory:"),
                    escalation=EscalationConfig(),
                    capacity_routing=CapacityRoutingConfig(),
                ),
            ),
            patch(
                "flows.agent_monitor.list_sessions",
                new_callable=AsyncMock,
                return_value=["feynman", "ike"],
            ),
            patch("flows.agent_monitor._sync_dependencies", new_callable=AsyncMock),
            patch(
                "flows.agent_monitor.check_for_stalls",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "flows.agent_monitor.check_for_unexpected_offline",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("flows.agent_monitor.get_agent_state", side_effect=mock_get_state),
            patch(
                "flows.agent_monitor.check_pending_issues",
                new_callable=AsyncMock,
                return_value=[pending_issue],
            ),
            patch(
                "flows.agent_monitor.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ),
            patch("flows.agent_monitor.has_commented_on_issue", return_value=False),
            patch("flows.agent_monitor.BackboneDB") as mock_db_cls,
        ):
            mock_db = AsyncMock()
            mock_db.is_acknowledged.return_value = False
            mock_db.query_deliveries.return_value = []
            mock_db_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_db_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await monitor_agents.fn()

        # Feynman is busy → deferred, Ike is idle → delivered
        assert result["feynman"] == "deferred"
        assert result["ike"] == f"delivered_#{pending_issue.number}"

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

        with (
            patch(
                "flows.agent_monitor.BackboneConfig.from_toml",
                return_value=BackboneConfig(
                    github_token="test",
                    webhook_secret="test",
                    entities=EntityConfig(
                        skip=frozenset({"elias"}),
                    ),
                    registry=_make_registry(["ike"]),
                    delivery=DeliveryConfig(db_path=":memory:"),
                    escalation=EscalationConfig(),
                    capacity_routing=CapacityRoutingConfig(),
                ),
            ),
            patch(
                "flows.agent_monitor.list_sessions",
                new_callable=AsyncMock,
                return_value=["ike"],
            ),
            patch("flows.agent_monitor._sync_dependencies", new_callable=AsyncMock),
            patch(
                "flows.agent_monitor.check_for_stalls",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "flows.agent_monitor.check_for_unexpected_offline",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "flows.agent_monitor.get_agent_state",
                new_callable=AsyncMock,
                return_value=idle_snapshot,
            ),
            patch(
                "flows.agent_monitor.check_pending_issues",
                new_callable=AsyncMock,
                return_value=[pending_issue],
            ),
            patch(
                "flows.agent_monitor.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
            patch("flows.agent_monitor.has_commented_on_issue", return_value=False),
            patch("flows.agent_monitor.BackboneDB") as mock_db_cls,
        ):
            mock_db = AsyncMock()
            mock_db.is_acknowledged.return_value = False
            mock_db.query_deliveries.return_value = []
            mock_db_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_db_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await monitor_agents.fn()

        assert result["ike"] == "delivered_#10"
        mock_deliver.assert_called_once()

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
        }

        mock_db = AsyncMock()
        mock_db.is_acknowledged.return_value = False
        mock_db.query_deliveries.return_value = [recent_delivery]
        mock_db.record_delivery = AsyncMock()

        with (
            patch(
                "flows.agent_monitor.BackboneConfig.from_toml",
                return_value=BackboneConfig(
                    github_token="test",
                    webhook_secret="test",
                    entities=EntityConfig(
                        skip=frozenset({"elias"}),
                    ),
                    registry=_make_registry(["ike"]),
                    delivery=DeliveryConfig(db_path=":memory:"),
                    escalation=EscalationConfig(),
                    capacity_routing=CapacityRoutingConfig(),
                    scheduling=SchedulingConfig(monitor_interval_seconds=60),
                ),
            ),
            patch(
                "flows.agent_monitor.list_sessions",
                new_callable=AsyncMock,
                return_value=["ike"],
            ),
            patch("flows.agent_monitor._sync_dependencies", new_callable=AsyncMock),
            patch(
                "flows.agent_monitor.check_for_stalls",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "flows.agent_monitor.check_for_unexpected_offline",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "flows.agent_monitor.get_agent_state",
                new_callable=AsyncMock,
                return_value=idle_snapshot,
            ),
            patch(
                "flows.agent_monitor.check_pending_issues",
                new_callable=AsyncMock,
                return_value=[pending_issue],
            ),
            patch(
                "flows.agent_monitor.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
            patch("flows.agent_monitor.has_commented_on_issue", return_value=False),
            patch("flows.agent_monitor.BackboneDB") as mock_db_cls,
        ):
            mock_db_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_db_cls.return_value.__aexit__ = AsyncMock(return_value=False)

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

        mock_db = AsyncMock()
        mock_db.is_acknowledged.return_value = True

        with (
            patch(
                "flows.agent_monitor.BackboneConfig.from_toml",
                return_value=BackboneConfig(
                    github_token="test",
                    webhook_secret="test",
                    entities=EntityConfig(
                        skip=frozenset({"elias"}),
                    ),
                    registry=_make_registry(["ike"]),
                    delivery=DeliveryConfig(db_path=":memory:"),
                    escalation=EscalationConfig(),
                    capacity_routing=CapacityRoutingConfig(),
                ),
            ),
            patch(
                "flows.agent_monitor.list_sessions",
                new_callable=AsyncMock,
                return_value=["ike"],
            ),
            patch("flows.agent_monitor._sync_dependencies", new_callable=AsyncMock),
            patch(
                "flows.agent_monitor.check_for_stalls",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "flows.agent_monitor.check_for_unexpected_offline",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "flows.agent_monitor.get_agent_state",
                new_callable=AsyncMock,
                return_value=idle_snapshot,
            ),
            patch(
                "flows.agent_monitor.check_pending_issues",
                new_callable=AsyncMock,
                return_value=[pending_issue],
            ),
            patch(
                "flows.agent_monitor.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
            patch("flows.agent_monitor.BackboneDB") as mock_db_cls,
        ):
            mock_db_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_db_cls.return_value.__aexit__ = AsyncMock(return_value=False)

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

        mock_db = AsyncMock()
        mock_db.is_acknowledged.return_value = False

        with (
            patch(
                "flows.agent_monitor.BackboneConfig.from_toml",
                return_value=BackboneConfig(
                    github_token="test",
                    webhook_secret="test",
                    entities=EntityConfig(
                        skip=frozenset({"elias"}),
                    ),
                    registry=_make_registry(["ike"]),
                    delivery=DeliveryConfig(db_path=":memory:"),
                    escalation=EscalationConfig(),
                    capacity_routing=CapacityRoutingConfig(),
                ),
            ),
            patch(
                "flows.agent_monitor.list_sessions",
                new_callable=AsyncMock,
                return_value=["ike"],
            ),
            patch("flows.agent_monitor._sync_dependencies", new_callable=AsyncMock),
            patch(
                "flows.agent_monitor.check_for_stalls",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "flows.agent_monitor.check_for_unexpected_offline",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "flows.agent_monitor.get_agent_state",
                new_callable=AsyncMock,
                return_value=idle_snapshot,
            ),
            patch(
                "flows.agent_monitor.check_pending_issues",
                new_callable=AsyncMock,
                return_value=[pending_issue],
            ),
            patch(
                "flows.agent_monitor.has_commented_on_issue",
                return_value=True,
            ),
            patch(
                "flows.agent_monitor.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
            patch("flows.agent_monitor.BackboneDB") as mock_db_cls,
        ):
            mock_db_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_db_cls.return_value.__aexit__ = AsyncMock(return_value=False)

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

        mock_db = AsyncMock()
        # First issue acknowledged, second is not
        mock_db.is_acknowledged.side_effect = lambda num, entity: num == 49
        mock_db.query_deliveries.return_value = []

        with (
            patch(
                "flows.agent_monitor.BackboneConfig.from_toml",
                return_value=BackboneConfig(
                    github_token="test",
                    webhook_secret="test",
                    entities=EntityConfig(
                        skip=frozenset({"elias"}),
                    ),
                    registry=_make_registry(["ike"]),
                    delivery=DeliveryConfig(db_path=":memory:"),
                    escalation=EscalationConfig(),
                    capacity_routing=CapacityRoutingConfig(),
                    scheduling=SchedulingConfig(monitor_interval_seconds=60),
                ),
            ),
            patch(
                "flows.agent_monitor.list_sessions",
                new_callable=AsyncMock,
                return_value=["ike"],
            ),
            patch("flows.agent_monitor._sync_dependencies", new_callable=AsyncMock),
            patch(
                "flows.agent_monitor.check_for_stalls",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "flows.agent_monitor.check_for_unexpected_offline",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "flows.agent_monitor.get_agent_state",
                new_callable=AsyncMock,
                return_value=idle_snapshot,
            ),
            patch(
                "flows.agent_monitor.check_pending_issues",
                new_callable=AsyncMock,
                return_value=[acked_issue, pending_issue],
            ),
            patch("flows.agent_monitor.has_commented_on_issue", return_value=False),
            patch(
                "flows.agent_monitor.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
            patch("flows.agent_monitor.BackboneDB") as mock_db_cls,
        ):
            mock_db_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_db_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await monitor_agents.fn()

        # Should skip #49 (acknowledged) and deliver #50
        assert result["ike"] == "delivered_#50"
        mock_deliver.assert_called_once()


class TestOfflineDedup:
    @pytest.mark.asyncio
    async def test_offline_clears_db_state(self):
        """After notifying about an offline agent, DB state is set to 'unknown'
        so the next monitor cycle doesn't re-detect the same offline event."""
        idle_snapshot = StateSnapshot(
            state=AgentState.IDLE,
            source="pull",
        )

        mock_db = AsyncMock()
        mock_db.is_acknowledged.return_value = False
        mock_db.query_deliveries.return_value = []

        with (
            patch(
                "flows.agent_monitor.BackboneConfig.from_toml",
                return_value=BackboneConfig(
                    github_token="test",
                    webhook_secret="test",
                    entities=EntityConfig(
                        skip=frozenset({"elias"}),
                    ),
                    registry=_make_registry(["ike", "feynman"]),
                    delivery=DeliveryConfig(db_path=":memory:"),
                    escalation=EscalationConfig(
                        escalation_target="ike",
                        escalation_dedup_seconds=1800,
                    ),
                    capacity_routing=CapacityRoutingConfig(),
                ),
            ),
            patch(
                "flows.agent_monitor.list_sessions",
                new_callable=AsyncMock,
                return_value=["ike"],  # feynman is NOT active
            ),
            patch("flows.agent_monitor._sync_dependencies", new_callable=AsyncMock),
            patch(
                "flows.agent_monitor.check_for_stalls",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "flows.agent_monitor.check_for_unexpected_offline",
                new_callable=AsyncMock,
                return_value=[
                    {"entity": "feynman", "session": "feynman", "pending_count": 0},
                ],
            ),
            patch(
                "flows.agent_monitor.get_agent_state",
                new_callable=AsyncMock,
                return_value=idle_snapshot,
            ),
            patch(
                "flows.agent_monitor.check_pending_issues",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "flows.agent_monitor.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
            patch("flows.agent_monitor.BackboneDB") as mock_db_cls,
        ):
            mock_db_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_db_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await monitor_agents.fn()

        # Escalation message was sent to ike via safe_deliver
        mock_deliver.assert_called()

        # DB state for feynman was cleared to "unknown"
        mock_db.set_agent_state.assert_called_once_with(
            session_name="feynman",
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
        with (
            patch(
                "flows.agent_monitor.get_agent_state",
                new_callable=AsyncMock,
                return_value=plan_snapshot,
            ),
            patch("flows.agent_monitor.BackboneDB") as mock_db_cls,
        ):
            mock_db = AsyncMock()
            mock_db_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_db_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            stalls = await check_for_stalls.fn(escalation_config, {"ike", "feynman", "leo"})

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
            github_token="test-token",
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
            delivery=DeliveryConfig(db_path=":memory:"),
            capacity_routing=CapacityRoutingConfig(),
            telegram=TelegramConfig(notification_chat_id=897573812),
        )

        with (
            patch(
                "flows.agent_monitor.BackboneConfig.from_toml",
                return_value=config_with_telegram,
            ),
            patch(
                "flows.agent_monitor.list_sessions",
                new_callable=AsyncMock,
                return_value=["feynman", "ike"],
            ),
            patch("flows.agent_monitor._sync_dependencies", new_callable=AsyncMock),
            patch(
                "flows.agent_monitor.check_for_stalls",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "flows.agent_monitor.check_for_unexpected_offline",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("flows.agent_monitor.get_agent_state", side_effect=mock_get_state),
            patch(
                "flows.agent_monitor.check_pending_issues",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "flows.agent_monitor.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ),
            patch("flows.agent_monitor.BackboneDB") as mock_db_cls,
            patch.dict(os.environ, {"TELEGRAM_TOKEN": "test-token"}),
            patch(
                "flows.agent_monitor.BackboneBot.send_notification",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_notify,
        ):
            mock_db = AsyncMock()
            mock_db.is_acknowledged.return_value = False
            mock_db.query_deliveries.return_value = []
            mock_db_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_db_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await monitor_agents.fn()

        mock_notify.assert_called_once()
        call_args = mock_notify.call_args[0]
        assert call_args[0] == "test-token"
        assert call_args[1] == 897573812
        assert "feynman" in call_args[2].lower() or "feynman" in call_args[2]
        assert "Add caching" in call_args[2]
        # feynman deferred (plan_waiting), ike delivered or no_pending
        assert result.get("feynman") == "deferred"
