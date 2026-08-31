"""Tests for session intelligence, target resolution and safe_deliver."""

from __future__ import annotations

import contextlib
import time
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.config import DeliveryConfig
from agent_backbone.services.agents import AgentState, StateSnapshot
from agent_backbone.services.routing import (
    SessionIntelligence,
    SessionProfile,
    get_session_intelligence,
    list_sessions_full,
    resolve_entity_session,
    resolve_entity_sessions,
    safe_deliver,
)
from agent_backbone.services.routing._resolution import validate_issue_targets

_IDLE_SNAP = StateSnapshot(state=AgentState.IDLE, source="push")
_BUSY_SNAP = StateSnapshot(state=AgentState.BUSY, source="push")
_PROCESSING_SNAP = StateSnapshot(state=AgentState.PROCESSING_ISSUE, source="push")
_PLAN_WAITING_SNAP = StateSnapshot(state=AgentState.PLAN_WAITING, source="push")
_PERMISSION_WAITING_SNAP = StateSnapshot(state=AgentState.PERMISSION_WAITING, source="push")
_UNKNOWN_SNAP = StateSnapshot(state=AgentState.UNKNOWN, source="default")
_PROCESSING_ISSUE_42_SNAP = StateSnapshot(
    state=AgentState.PROCESSING_ISSUE, current_issue=42, source="push"
)
_PROCESSING_ISSUE_99_SNAP = StateSnapshot(
    state=AgentState.PROCESSING_ISSUE, current_issue=99, source="push"
)
_PLAN_WAITING_ISSUE_42_SNAP = StateSnapshot(
    state=AgentState.PLAN_WAITING, current_issue=42, source="push"
)
_PERMISSION_WAITING_ISSUE_42_SNAP = StateSnapshot(
    state=AgentState.PERMISSION_WAITING, current_issue=42, source="push"
)

_INTEL = "agent_backbone.services.routing._intelligence"
_DELIV = "agent_backbone.services.routing._delivery"


@pytest.fixture(autouse=True)
def _patch_tmux_runtime_env():
    """Keep runtime resolution inside unit tests by disabling tmux env lookups."""
    with patch(
        "agent_backbone.services.terminal._adapters.query_environment_var",
        new_callable=AsyncMock,
        return_value=None,
    ):
        yield


@pytest.fixture(autouse=True)
def _patch_default_capture_pane():
    with patch(f"{_INTEL}.capture_pane", new_callable=AsyncMock, return_value=""):
        yield


def _patch_list_sessions(sessions: list[str]):
    return patch(f"{_INTEL}.list_sessions", new_callable=AsyncMock, return_value=sessions)


def _patch_query_format_vars(vars_dict: dict[str, str]):
    return patch(f"{_INTEL}.query_format_vars", new_callable=AsyncMock, return_value=vars_dict)


def _patch_get_agent_state(snap: StateSnapshot):
    return patch(f"{_INTEL}.get_agent_state", new_callable=AsyncMock, return_value=snap)


def _patch_capture_pane(content: str):
    return patch(f"{_INTEL}.capture_pane", new_callable=AsyncMock, return_value=content)


def _patch_send_message(success: bool = True):
    return patch(f"{_DELIV}.send_message", new_callable=AsyncMock, return_value=success)


@contextlib.contextmanager
def _online(session: str = "ike", snap: StateSnapshot = _IDLE_SNAP):
    """An online session (not in copy mode) reporting the given agent state."""
    with (
        _patch_list_sessions([session]),
        _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
        _patch_get_agent_state(snap),
    ):
        yield


# ---------------------------------------------------------------------------
# get_session_intelligence
# ---------------------------------------------------------------------------


class TestGetSessionIntelligence:
    async def test_offline_when_session_not_active(self, config):
        with _patch_list_sessions([]):
            profile = await get_session_intelligence("ike", config)
        assert profile.intelligence == SessionIntelligence.OFFLINE
        assert profile.agent_state == AgentState.UNKNOWN

    async def test_copy_mode(self, config):
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "1", "client_activity": "0"}),
            _patch_get_agent_state(_IDLE_SNAP),
        ):
            profile = await get_session_intelligence("ike", config)
        assert profile.intelligence == SessionIntelligence.COPY_MODE
        assert profile.tmux_vars["pane_in_mode"] == "1"

    async def test_recent_client_activity_does_not_trigger_user_interacting(self, config):
        recent = str(time.time() - 2)
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": recent}),
            _patch_get_agent_state(_IDLE_SNAP),
        ):
            profile = await get_session_intelligence("ike", config)
        assert profile.intelligence == SessionIntelligence.IDLE_READY

    async def test_user_interacting_when_prompt_has_buffered_input(self, config):
        with _online(), _patch_capture_pane("› Review the routing fallback logic"):
            profile = await get_session_intelligence("ike", config)
        assert profile.intelligence == SessionIntelligence.USER_INTERACTING

    async def test_user_interacting_when_codex_status_line_is_below_prompt(self, config):
        pane = (
            "› Review the routing fallback logic\n\n  gpt-5.4 xhigh · 91% left · ~/code/dashboard"
        )
        with _online(), _patch_capture_pane(pane):
            profile = await get_session_intelligence("ike", config)
        assert profile.intelligence == SessionIntelligence.USER_INTERACTING

    async def test_codex_placeholder_prompt_is_idle_ready(self, config):
        pane = "\x1b[1m›\x1b[0m \x1b[2mImprove documentation in @filename\x1b[0m"
        with _online(), _patch_capture_pane(pane):
            profile = await get_session_intelligence("ike", config)
        assert profile.intelligence == SessionIntelligence.IDLE_READY

    async def test_codex_placeholder_with_status_line_is_idle_ready(self, config):
        pane = (
            "\x1b[1m›\x1b[0m \x1b[2mImprove documentation in @filename\x1b[0m\n\n"
            "  gpt-5.4 xhigh · 91% left · ~/code/dashboard"
        )
        with _online(), _patch_capture_pane(pane):
            profile = await get_session_intelligence("ike", config)
        assert profile.intelligence == SessionIntelligence.IDLE_READY

    @pytest.mark.parametrize(
        ("snap", "expected"),
        [
            (_PLAN_WAITING_SNAP, SessionIntelligence.PLAN_WAITING),
            (_PERMISSION_WAITING_SNAP, SessionIntelligence.PERMISSION_WAITING),
            (_BUSY_SNAP, SessionIntelligence.AGENT_WORKING),
            (_PROCESSING_SNAP, SessionIntelligence.AGENT_WORKING),
            (_IDLE_SNAP, SessionIntelligence.IDLE_READY),
            (_UNKNOWN_SNAP, SessionIntelligence.UNKNOWN),
        ],
    )
    async def test_agent_state_mapping(self, config, snap, expected):
        with _online(snap=snap):
            profile = await get_session_intelligence("ike", config)
        assert profile.intelligence == expected
        assert profile.agent_state == snap.state

    @pytest.mark.parametrize("snap", [_PLAN_WAITING_SNAP, _BUSY_SNAP, _PROCESSING_SNAP])
    async def test_copy_mode_does_not_mask_agent_states(self, config, snap):
        """Agent-reported states must outrank tmux-only copy-mode signals."""
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "1", "client_activity": "0"}),
            _patch_get_agent_state(snap),
        ):
            profile = await get_session_intelligence("ike", config)
        assert profile.intelligence != SessionIntelligence.COPY_MODE

    async def test_busy_profile_drops_stale_current_issue(self, config):
        busy_with_issue = StateSnapshot(state=AgentState.BUSY, current_issue=42, source="push")
        with _online(snap=busy_with_issue):
            profile = await get_session_intelligence("ike", config)
        assert profile.current_issue is None

    async def test_idle_grace(self, config):
        config = replace(config, delivery=DeliveryConfig(grace_period_seconds=5))
        with _online():
            profile = await get_session_intelligence("ike", config, idle_since=time.monotonic() - 1)
        assert profile.intelligence == SessionIntelligence.IDLE_GRACE

    async def test_idle_grace_elapsed(self, config):
        config = replace(config, delivery=DeliveryConfig(grace_period_seconds=1))
        with _online():
            profile = await get_session_intelligence(
                "ike", config, idle_since=time.monotonic() - 10
            )
        assert profile.intelligence == SessionIntelligence.IDLE_READY


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


class TestResolveEntitySession:
    def test_configured_agent(self, config):
        assert resolve_entity_session("ike", config) == "ike"
        assert resolve_entity_sessions("ike", config) == ["ike"]

    def test_ignored_target(self, config):
        assert resolve_entity_session("elias", config) is None
        assert resolve_entity_sessions("elias", config) == []

    def test_unknown_target(self, config):
        assert resolve_entity_session("nobody", config) is None

    def test_validate_issue_targets_accepts_agents_and_ignored(self, config):
        validate_issue_targets(["ike", "elias"], config)

    def test_validate_issue_targets_rejects_unknown(self, config):
        with pytest.raises(ValueError, match="unknown issue target\\(s\\): nobody"):
            validate_issue_targets(["ike", "nobody"], config)


# ---------------------------------------------------------------------------
# safe_deliver
# ---------------------------------------------------------------------------


class TestSafeDeliver:
    async def test_delivered_idle_ready(self, config):
        with _online(), _patch_send_message(True) as mock_send:
            result = await safe_deliver("ike", "Hello", config)
        assert result == "delivered"
        mock_send.assert_called_once_with("ike", "Hello", runtime_hint="unknown")

    async def test_offline_enqueues(self, config):
        mock_db = AsyncMock()
        with _patch_list_sessions([]):
            result = await safe_deliver(
                "ike",
                "Hello",
                config,
                db=mock_db,
                issue_number=42,
                target_entity="ike",
                flow_name="test_flow",
            )
        assert result == "offline"
        mock_db.enqueue_message.assert_called_once_with(
            session_name="ike",
            message="Hello",
            issue_number=42,
            target_entity="ike",
            delivery_kind="issue",
            flow_name="test_flow",
        )

    async def test_offline_no_enqueue_without_issue_number(self, config):
        mock_db = AsyncMock()
        with _patch_list_sessions([]):
            result = await safe_deliver("ike", "Hello", config, db=mock_db)
        assert result == "offline"
        mock_db.enqueue_message.assert_not_called()

    async def test_copy_mode_recovers_and_delivers(self, config):
        copy_profile = SessionProfile("ike", SessionIntelligence.COPY_MODE, runtime="codex")
        idle_profile = SessionProfile("ike", SessionIntelligence.IDLE_READY, runtime="codex")
        adapter = AsyncMock()
        adapter.exit_copy_mode.return_value = True
        with (
            patch(
                f"{_DELIV}.get_session_intelligence",
                new_callable=AsyncMock,
                side_effect=[copy_profile, idle_profile],
            ),
            patch(f"{_DELIV}.get_terminal_adapter", return_value=adapter),
            _patch_send_message(True),
        ):
            result = await safe_deliver("ike", "Hello", config)
        assert result == "delivered"
        adapter.exit_copy_mode.assert_awaited_once_with("ike")

    async def test_persistent_copy_mode_fails_and_enqueues(self, config):
        mock_db = AsyncMock()
        copy_profile = SessionProfile("ike", SessionIntelligence.COPY_MODE, runtime="codex")
        adapter = AsyncMock()
        adapter.exit_copy_mode.return_value = True
        with (
            patch(
                f"{_DELIV}.get_session_intelligence",
                new_callable=AsyncMock,
                side_effect=[copy_profile, copy_profile],
            ),
            patch(f"{_DELIV}.get_terminal_adapter", return_value=adapter),
        ):
            result = await safe_deliver(
                "ike",
                "Hello",
                config,
                db=mock_db,
                issue_number=42,
                target_entity="ike",
                flow_name="test_flow",
            )
        assert result == "delivery_failed"
        mock_db.enqueue_message.assert_called_once()
        mock_db.record_delivery.assert_called_once_with(
            issue_number=42,
            target_entity="ike",
            session_name="ike",
            outcome="delivery_failed",
            flow_name="test_flow",
        )

    async def test_user_interacting_blocks(self, config):
        with _online(), _patch_capture_pane("› Review the fallback routing logic"):
            result = await safe_deliver("ike", "Hello", config, priority=False)
        assert result == "user_interacting"

    async def test_user_interacting_priority_bypasses(self, config):
        with (
            _online(),
            _patch_capture_pane("› Review the fallback routing logic"),
            _patch_send_message(True),
        ):
            result = await safe_deliver("ike", "Hello", config, priority=True)
        assert result == "delivered"

    async def test_user_interacting_enqueues(self, config):
        mock_db = AsyncMock()
        with _online(), _patch_capture_pane("› Review the fallback routing logic"):
            result = await safe_deliver(
                "ike",
                "Hello",
                config,
                db=mock_db,
                issue_number=42,
                target_entity="ike",
                flow_name="test_flow",
            )
        assert result == "user_interacting"
        mock_db.enqueue_message.assert_called_once()

    async def test_agent_working_blocks(self, config):
        with _online(snap=_BUSY_SNAP):
            result = await safe_deliver("ike", "Hello", config)
        assert result == "agent_working"

    async def test_direct_message_defers_durably_while_agent_working(self, config):
        mock_db = AsyncMock()
        with _online(snap=_BUSY_SNAP):
            result = await safe_deliver(
                "ike",
                "Hello",
                config,
                db=mock_db,
                flow_name="api-messages",
                delivery_kind="direct_message",
            )
        assert result == "agent_working"
        mock_db.enqueue_message.assert_called_once_with(
            session_name="ike",
            message="Hello",
            issue_number=None,
            target_entity=None,
            delivery_kind="direct_message",
            flow_name="api-messages",
        )

    @pytest.mark.parametrize(
        ("snap", "outcome"),
        [(_PLAN_WAITING_SNAP, "plan_waiting"), (_PERMISSION_WAITING_SNAP, "permission_waiting")],
    )
    async def test_waiting_states_block_even_priority(self, config, snap, outcome):
        with _online(snap=snap):
            result = await safe_deliver("ike", "Hello", config, priority=True)
        assert result == outcome

    async def test_delivery_failed_enqueues(self, config):
        mock_db = AsyncMock()
        with _online(), _patch_send_message(False):
            result = await safe_deliver(
                "ike",
                "Hello",
                config,
                db=mock_db,
                issue_number=42,
                target_entity="ike",
                flow_name="test_flow",
            )
        assert result == "delivery_failed"
        mock_db.enqueue_message.assert_called_once()

    async def test_unknown_state_still_delivers(self, config):
        mock_db = AsyncMock()
        with (
            _online(snap=_UNKNOWN_SNAP),
            _patch_send_message(True),
        ):
            result = await safe_deliver(
                "ike",
                "Hello",
                config,
                db=mock_db,
                issue_number=42,
                target_entity="ike",
                flow_name="test_flow",
            )
        assert result == "delivered"
        mock_db.enqueue_message.assert_not_called()
        mock_db.record_delivery.assert_called_once_with(
            issue_number=42,
            target_entity="ike",
            session_name="ike",
            outcome="delivered",
            flow_name="test_flow",
        )

    async def test_enforce_issue_queue_blocks_duplicate_issue(self, config):
        mock_db = AsyncMock()
        mock_db.query_deliveries.return_value = [
            {
                "issue_number": 42,
                "target_entity": "ike",
                "session_name": "ike",
                "outcome": "delivered",
            }
        ]
        result = await safe_deliver(
            "ike",
            "Hello",
            config,
            db=mock_db,
            issue_number=42,
            target_entity="ike",
            flow_name="test_flow",
            enforce_issue_queue=True,
        )
        assert result == "already_delivered"
        mock_db.record_delivery.assert_not_called()

    async def test_enforce_issue_queue_blocks_until_acknowledged(self, config):
        mock_db = AsyncMock()
        mock_db.query_deliveries.side_effect = [
            [],
            [
                {
                    "issue_number": 41,
                    "target_entity": "ike",
                    "session_name": "ike",
                    "outcome": "delivered",
                }
            ],
        ]
        mock_db.is_acknowledged.return_value = False
        result = await safe_deliver(
            "ike",
            "Hello",
            config,
            db=mock_db,
            issue_number=42,
            target_entity="ike",
            flow_name="test_flow",
            enforce_issue_queue=True,
        )
        assert result == "awaiting_ack"
        mock_db.record_delivery.assert_not_called()

    async def test_enforce_issue_queue_ignores_stale_delivery_outside_open_queue(self, config):
        mock_db = AsyncMock()
        mock_db.query_deliveries.side_effect = [
            [],
            [
                {
                    "issue_number": 665,
                    "target_entity": "ike",
                    "session_name": "ike",
                    "outcome": "delivered",
                }
            ],
        ]
        mock_db.is_acknowledged.return_value = False
        with _online(), _patch_send_message(True):
            result = await safe_deliver(
                "ike",
                "Hello",
                config,
                db=mock_db,
                issue_number=42,
                target_entity="ike",
                flow_name="test_flow",
                enforce_issue_queue=True,
                queue_scope_issue_numbers={42, 43},
            )
        assert result == "delivered"

    async def test_safe_deliver_claims_before_send(self, config):
        mock_db = AsyncMock()
        mock_db.query_deliveries.return_value = []
        order: list[str] = []

        async def _claim(*args, **kwargs):
            order.append("claim")
            return 123

        async def _send(*args, **kwargs):
            order.append("send")
            return True

        async def _finalize(*args, **kwargs):
            order.append("finalize")

        mock_db.claim_delivery_attempt = AsyncMock(side_effect=_claim)
        mock_db.finalize_delivery_attempt = AsyncMock(side_effect=_finalize)
        with _online(), patch(f"{_DELIV}.send_message", new_callable=AsyncMock, side_effect=_send):
            result = await safe_deliver(
                "ike",
                "Hello",
                config,
                db=mock_db,
                issue_number=42,
                target_entity="ike",
                flow_name="test_flow",
            )
        assert result == "delivered"
        assert order == ["claim", "send", "finalize"]
        mock_db.finalize_delivery_attempt.assert_awaited_once_with(123, "delivered")
        mock_db.record_delivery.assert_not_called()

    async def test_safe_deliver_claim_conflict_returns_already_delivered(self, config):
        mock_db = AsyncMock()
        mock_db.query_deliveries.return_value = []
        mock_db.claim_delivery_attempt.return_value = None
        with (
            patch(f"{_DELIV}.get_session_intelligence", new_callable=AsyncMock) as intel,
            patch(f"{_DELIV}.send_message", new_callable=AsyncMock) as send,
        ):
            result = await safe_deliver(
                "ike",
                "Hello",
                config,
                db=mock_db,
                issue_number=42,
                target_entity="ike",
                flow_name="test_flow",
            )
        assert result == "already_delivered"
        intel.assert_not_called()
        send.assert_not_called()

    async def test_comment_delivery_records_distinct_outcome(self, config):
        mock_db = AsyncMock()
        with _online(), _patch_send_message(True):
            result = await safe_deliver(
                "ike",
                "Hello",
                config,
                db=mock_db,
                issue_number=42,
                target_entity="ike",
                flow_name="test_flow",
                delivery_kind="comment",
            )
        assert result == "delivered"
        mock_db.record_delivery.assert_called_once_with(
            issue_number=42,
            target_entity="ike",
            session_name="ike",
            outcome="comment_delivered",
            flow_name="test_flow",
        )

    @pytest.mark.parametrize(
        "snap",
        [_PROCESSING_ISSUE_42_SNAP, _PLAN_WAITING_ISSUE_42_SNAP, _PERMISSION_WAITING_ISSUE_42_SNAP],
    )
    async def test_comment_delivery_bypasses_blocking_state_for_current_issue(self, config, snap):
        mock_db = AsyncMock()
        with (
            _online(snap=snap),
            _patch_send_message(True),
        ):
            result = await safe_deliver(
                "ike",
                "Comment",
                config,
                db=mock_db,
                issue_number=42,
                target_entity="ike",
                flow_name="test_flow",
                delivery_kind="comment",
            )
        assert result == "delivered"

    async def test_comment_delivery_to_different_issue_is_queued_while_busy(self, config):
        mock_db = AsyncMock()
        with (
            _online(snap=_PROCESSING_ISSUE_99_SNAP),
        ):
            result = await safe_deliver(
                "ike",
                "Comment",
                config,
                db=mock_db,
                issue_number=42,
                target_entity="ike",
                flow_name="test_flow",
                delivery_kind="comment",
            )
        assert result == "agent_working"
        mock_db.enqueue_message.assert_called_once()
        mock_db.record_delivery.assert_called_once_with(
            issue_number=42,
            target_entity="ike",
            session_name="ike",
            outcome="comment_agent_working",
            flow_name="test_flow",
        )

    async def test_grace_period_defers(self, config):
        with patch(
            f"{_DELIV}.get_session_intelligence",
            new_callable=AsyncMock,
            return_value=SessionProfile("ike", SessionIntelligence.IDLE_GRACE),
        ):
            assert await safe_deliver("ike", "Hello", config) == "grace_period"

    async def test_idle_grace_enqueues_non_issue_but_not_issue(self, config):
        mock_db = AsyncMock()
        with patch(
            f"{_DELIV}.get_session_intelligence",
            new_callable=AsyncMock,
            return_value=SessionProfile("ike", SessionIntelligence.IDLE_GRACE),
        ):
            comment = await safe_deliver(
                "ike",
                "Comment",
                config,
                db=mock_db,
                issue_number=42,
                target_entity="ike",
                delivery_kind="comment",
            )
            mock_db.enqueue_message.assert_called_once()
            mock_db.enqueue_message.reset_mock()
            issue = await safe_deliver(
                "ike",
                "Issue",
                config,
                db=mock_db,
                issue_number=42,
                target_entity="ike",
            )
            mock_db.enqueue_message.assert_not_called()
        assert comment == "grace_period" and issue == "grace_period"


class TestListSessionsFull:
    async def test_enriches_sessions(self, config):
        mock_sessions = [
            {"name": "ike", "windows": 1, "created": 1000, "attached": True},
            {"name": "leo", "windows": 2, "created": 2000, "attached": False},
        ]
        with (
            patch(
                "agent_backbone.services.terminal.list_sessions_rich",
                new_callable=AsyncMock,
                return_value=mock_sessions,
            ),
            _patch_list_sessions(["ike", "leo"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_IDLE_SNAP),
        ):
            result = await list_sessions_full(config)

        assert [r["name"] for r in result] == ["ike", "leo"]
        assert result[0]["intelligence"] == "idle_ready"
        assert result[0]["agent_state"] == "idle"
        assert result[0]["windows"] == 1 and result[0]["attached"] is True

    async def test_empty_list(self, config):
        with patch(
            "agent_backbone.services.terminal.list_sessions_rich",
            new_callable=AsyncMock,
            return_value=[],
        ):
            assert await list_sessions_full(config) == []
