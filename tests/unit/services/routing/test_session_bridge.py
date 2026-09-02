"""Tests for session intelligence, target resolution and safe_deliver."""

from __future__ import annotations

import contextlib
import time
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.config import DeliveryConfig
from agent_backbone.services.agents import AgentState, StateSnapshot
from agent_backbone.services.routing import get_session_intelligence, safe_deliver
from agent_backbone.services.routing._resolution import (
    resolve_entity_session,
    validate_issue_targets,
)
from agent_backbone.services.routing.models import SessionIntelligence, SessionProfile

_IDLE_SNAP = StateSnapshot(state=AgentState.IDLE, source="push")
_BUSY_SNAP = StateSnapshot(state=AgentState.BUSY, source="push")
_STARTING_SNAP = StateSnapshot(state=AgentState.STARTING, source="push")
_PLAN_SNAP = StateSnapshot(state=AgentState.WAITING_FOR_HUMAN, reason="plan", source="push")
_PERMISSION_SNAP = StateSnapshot(
    state=AgentState.WAITING_FOR_HUMAN, reason="permission", source="push"
)
_UNKNOWN_SNAP = StateSnapshot(state=AgentState.UNKNOWN, source="default")
_BUSY_ISSUE_42_SNAP = StateSnapshot(state=AgentState.BUSY, current_issue=42, source="push")
_BUSY_ISSUE_99_SNAP = StateSnapshot(state=AgentState.BUSY, current_issue=99, source="push")
_PLAN_ISSUE_42_SNAP = StateSnapshot(
    state=AgentState.WAITING_FOR_HUMAN, reason="plan", current_issue=42, source="push"
)

_INTEL = "agent_backbone.services.routing._intelligence"
_DELIV = "agent_backbone.services.routing._delivery"
_COPY = "agent_backbone.services.terminal._copy_mode"


@pytest.fixture(autouse=True)
def _patch_tmux_runtime_env():
    with patch(
        "agent_backbone.services.runtimes.query_environment_var",
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
    return patch(f"{_COPY}.query_format_vars", new_callable=AsyncMock, return_value=vars_dict)


def _patch_get_agent_state(snap: StateSnapshot):
    return patch(f"{_INTEL}.get_agent_state", new_callable=AsyncMock, return_value=snap)


def _patch_capture_pane(content: str):
    return patch(f"{_INTEL}.capture_pane", new_callable=AsyncMock, return_value=content)


def _patch_send_message(success: bool = True):
    return patch(f"{_DELIV}.send_message", new_callable=AsyncMock, return_value=success)


@contextlib.contextmanager
def _online(session: str = "ike", snap: StateSnapshot = _IDLE_SNAP):
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
        assert profile.evidence

    async def test_copy_mode_is_cleared_not_reported(self, config):
        # tmux reports copy mode until the cancel lands, then a clean pane.
        in_mode = [{"pane_in_mode": "1"}, {"pane_in_mode": "0"}]
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "1", "client_activity": "0"}),
            patch(f"{_COPY}.query_format_vars", new_callable=AsyncMock, side_effect=in_mode),
            patch(f"{_COPY}.asyncio.sleep", new_callable=AsyncMock),
            _patch_get_agent_state(_IDLE_SNAP),
            patch(
                f"{_COPY}.cancel_copy_mode",
                new_callable=AsyncMock,
                return_value=True,
            ) as exit_copy,
        ):
            profile = await get_session_intelligence("ike", config)
        exit_copy.assert_awaited_once_with("ike")
        assert profile.intelligence == SessionIntelligence.READY
        assert any("copy mode" in line for line in profile.evidence)

    async def test_copy_mode_that_will_not_clear_reads_as_human_typing(self, config):
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "1", "client_activity": "0"}),
            patch(
                f"{_COPY}.query_format_vars",
                new_callable=AsyncMock,
                return_value={"pane_in_mode": "1"},
            ),
            patch(f"{_COPY}.asyncio.sleep", new_callable=AsyncMock),
            _patch_get_agent_state(_IDLE_SNAP),
            patch(
                f"{_COPY}.cancel_copy_mode",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            profile = await get_session_intelligence("ike", config)
        assert profile.intelligence == SessionIntelligence.HUMAN_TYPING

    async def test_human_typing_when_prompt_has_buffered_input(self, config):
        with _online(), _patch_capture_pane("› Review the routing fallback logic"):
            profile = await get_session_intelligence("ike", config)
        assert profile.intelligence == SessionIntelligence.HUMAN_TYPING

    async def test_human_typing_when_codex_status_line_is_below_prompt(self, config):
        pane = (
            "› Review the routing fallback logic\n\n  gpt-5.4 xhigh · 91% left · ~/code/dashboard"
        )
        with _online(), _patch_capture_pane(pane):
            profile = await get_session_intelligence("ike", config)
        assert profile.intelligence == SessionIntelligence.HUMAN_TYPING

    async def test_codex_placeholder_prompt_is_ready(self, config):
        pane = "\x1b[1m›\x1b[0m \x1b[2mImprove documentation in @filename\x1b[0m"
        with _online(), _patch_capture_pane(pane):
            profile = await get_session_intelligence("ike", config)
        assert profile.intelligence == SessionIntelligence.READY

    @pytest.mark.parametrize(
        ("snap", "expected"),
        [
            (_PLAN_SNAP, SessionIntelligence.WAITING_FOR_HUMAN),
            (_PERMISSION_SNAP, SessionIntelligence.WAITING_FOR_HUMAN),
            (_BUSY_SNAP, SessionIntelligence.AGENT_WORKING),
            (_STARTING_SNAP, SessionIntelligence.AGENT_WORKING),
            (_IDLE_SNAP, SessionIntelligence.READY),
            (_UNKNOWN_SNAP, SessionIntelligence.UNKNOWN),
        ],
    )
    async def test_agent_state_mapping(self, config, snap, expected):
        with _online(snap=snap):
            profile = await get_session_intelligence("ike", config)
        assert profile.intelligence == expected
        assert profile.agent_state == snap.state
        assert profile.reason == snap.reason

    @pytest.mark.parametrize("snap", [_PLAN_SNAP, _BUSY_SNAP])
    async def test_copy_mode_does_not_mask_agent_states(self, config, snap):
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "1", "client_activity": "0"}),
            _patch_get_agent_state(snap),
            patch(
                f"{_COPY}.cancel_copy_mode",
                new_callable=AsyncMock,
            ) as exit_copy,
        ):
            profile = await get_session_intelligence("ike", config)
        exit_copy.assert_not_called()
        assert profile.intelligence in (
            SessionIntelligence.WAITING_FOR_HUMAN,
            SessionIntelligence.AGENT_WORKING,
        )

    async def test_busy_profile_keeps_current_issue(self, config):
        with _online(snap=_BUSY_ISSUE_42_SNAP):
            profile = await get_session_intelligence("ike", config)
        assert profile.current_issue == 42

    async def test_settling(self, config):
        config = replace(config, delivery=DeliveryConfig(grace_period_seconds=5))
        with _online():
            profile = await get_session_intelligence("ike", config, idle_since=time.monotonic() - 1)
        assert profile.intelligence == SessionIntelligence.SETTLING

    async def test_settling_elapsed(self, config):
        config = replace(config, delivery=DeliveryConfig(grace_period_seconds=1))
        with _online():
            profile = await get_session_intelligence(
                "ike", config, idle_since=time.monotonic() - 10
            )
        assert profile.intelligence == SessionIntelligence.READY


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


class TestResolveEntitySession:
    def test_configured_agent(self, config):
        assert resolve_entity_session("ike", config) == "ike"

    def test_ignored_target(self, config):
        assert resolve_entity_session("elias", config) is None

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


def _issue_kwargs(**extra):
    base = dict(repo="example/orchestration", issue_number=42, target_entity="ike", source="t")
    base.update(extra)
    return base


class TestSafeDeliver:
    async def test_delivered_ready(self, config):
        with _online(), _patch_send_message(True) as mock_send:
            result = await safe_deliver("ike", "Hello", config)
        assert result == "delivered"
        mock_send.assert_called_once_with("ike", "Hello", runtime_hint="unknown")

    async def test_offline_enqueues_issue(self, config):
        mock_db = AsyncMock()
        with _patch_list_sessions([]):
            result = await safe_deliver("ike", "Hello", config, db=mock_db, **_issue_kwargs())
        assert result == "offline"
        mock_db.enqueue_message.assert_called_once_with(
            session_name="ike",
            message="Hello",
            issue_number=42,
            target_entity="ike",
            delivery_kind="issue",
            source="t",
            repo="example/orchestration",
        )

    async def test_every_delivery_is_recorded_even_direct_messages(self, config):
        mock_db = AsyncMock()
        with _online(), _patch_send_message(True):
            result = await safe_deliver(
                "ike",
                "Hi there",
                config,
                db=mock_db,
                source="api",
                delivery_kind="direct_message",
            )
        assert result == "delivered"
        mock_db.record_delivery.assert_called_once_with(
            issue_number=None,
            target_entity="ike",
            session_name="ike",
            outcome="delivered",
            source="api",
            repo="",
            kind="direct_message",
            preview="Hi there",
        )

    async def test_human_typing_blocks(self, config):
        with _online(), _patch_capture_pane("› Review the fallback routing logic"):
            result = await safe_deliver("ike", "Hello", config, priority=False)
        assert result == "human_typing"

    async def test_human_typing_priority_bypasses(self, config):
        with (
            _online(),
            _patch_capture_pane("› Review the fallback routing logic"),
            _patch_send_message(True),
        ):
            result = await safe_deliver("ike", "Hello", config, priority=True)
        assert result == "delivered"

    async def test_human_typing_enqueues_non_issue(self, config):
        mock_db = AsyncMock()
        with _online(), _patch_capture_pane("› Review the fallback routing logic"):
            result = await safe_deliver(
                "ike", "Hello", config, db=mock_db, delivery_kind="direct_message"
            )
        assert result == "human_typing"
        mock_db.enqueue_message.assert_called_once()

    async def test_agent_working_blocks_even_priority(self, config):
        with _online(snap=_BUSY_SNAP):
            assert await safe_deliver("ike", "Hello", config, priority=True) == "agent_working"

    async def test_direct_message_queued_while_agent_working(self, config):
        mock_db = AsyncMock()
        with _online(snap=_BUSY_SNAP):
            result = await safe_deliver(
                "ike",
                "Hello",
                config,
                db=mock_db,
                source="api-messages",
                delivery_kind="direct_message",
            )
        assert result == "agent_working"
        mock_db.enqueue_message.assert_called_once_with(
            session_name="ike",
            message="Hello",
            issue_number=None,
            target_entity=None,
            delivery_kind="direct_message",
            source="api-messages",
            repo="",
        )

    @pytest.mark.parametrize("snap", [_PLAN_SNAP, _PERMISSION_SNAP])
    async def test_waiting_for_human_blocks_even_priority(self, config, snap):
        with _online(snap=snap):
            result = await safe_deliver("ike", "Hello", config, priority=True)
        assert result == "waiting_for_human"

    async def test_delivery_failed_enqueues(self, config):
        mock_db = AsyncMock()
        with _online(), _patch_send_message(False):
            result = await safe_deliver("ike", "Hello", config, db=mock_db, **_issue_kwargs())
        assert result == "delivery_failed"
        mock_db.enqueue_message.assert_called_once()

    async def test_unknown_state_still_delivers(self, config):
        mock_db = AsyncMock()
        with _online(snap=_UNKNOWN_SNAP), _patch_send_message(True):
            result = await safe_deliver("ike", "Hello", config, db=mock_db, **_issue_kwargs())
        assert result == "delivered"
        mock_db.enqueue_message.assert_not_called()
        mock_db.finalize_delivery_attempt.assert_called_once()

    async def test_enforce_issue_queue_blocks_duplicate_issue(self, config):
        mock_db = AsyncMock()
        mock_db.query_deliveries.return_value = [
            {
                "repo": "example/orchestration",
                "issue_number": 42,
                "target_entity": "ike",
                "session_name": "ike",
                "outcome": "delivered",
            }
        ]
        result = await safe_deliver(
            "ike", "Hello", config, db=mock_db, enforce_issue_queue=True, **_issue_kwargs()
        )
        assert result == "already_delivered"
        mock_db.record_delivery.assert_not_called()

    async def test_enforce_issue_queue_blocks_until_acknowledged(self, config):
        mock_db = AsyncMock()
        mock_db.query_deliveries.side_effect = [
            [],
            [
                {
                    "repo": "example/orchestration",
                    "issue_number": 41,
                    "target_entity": "ike",
                    "session_name": "ike",
                    "outcome": "delivered",
                }
            ],
        ]
        mock_db.is_acknowledged.return_value = False
        result = await safe_deliver(
            "ike", "Hello", config, db=mock_db, enforce_issue_queue=True, **_issue_kwargs()
        )
        assert result == "awaiting_ack"
        mock_db.record_delivery.assert_not_called()

    async def test_same_number_in_other_repo_does_not_block(self, config):
        mock_db = AsyncMock()
        mock_db.query_deliveries.side_effect = [
            [],
            [
                {
                    "repo": "example/other",
                    "issue_number": 41,
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
                enforce_issue_queue=True,
                queue_scope={("example/orchestration", 42)},
                **_issue_kwargs(),
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
            result = await safe_deliver("ike", "Hello", config, db=mock_db, **_issue_kwargs())
        assert result == "delivered"
        assert order == ["claim", "send", "finalize"]
        mock_db.finalize_delivery_attempt.assert_awaited_once_with(123, "delivered")
        assert mock_db.claim_delivery_attempt.await_args.kwargs["repo"] == "example/orchestration"
        mock_db.record_delivery.assert_not_called()

    async def test_safe_deliver_claim_conflict_returns_already_delivered(self, config):
        mock_db = AsyncMock()
        mock_db.query_deliveries.return_value = []
        mock_db.claim_delivery_attempt.return_value = None
        with (
            patch(f"{_DELIV}.get_session_intelligence", new_callable=AsyncMock) as intel,
            patch(f"{_DELIV}.send_message", new_callable=AsyncMock) as send,
        ):
            result = await safe_deliver("ike", "Hello", config, db=mock_db, **_issue_kwargs())
        assert result == "already_delivered"
        intel.assert_not_called()
        send.assert_not_called()

    async def test_comment_delivery_records_kind(self, config):
        mock_db = AsyncMock()
        with _online(), _patch_send_message(True):
            result = await safe_deliver(
                "ike", "Hello", config, db=mock_db, delivery_kind="comment", **_issue_kwargs()
            )
        assert result == "delivered"
        mock_db.record_delivery.assert_called_once_with(
            issue_number=42,
            target_entity="ike",
            session_name="ike",
            outcome="delivered",
            source="t",
            repo="example/orchestration",
            kind="comment",
            preview="Hello",
        )

    @pytest.mark.parametrize("snap", [_BUSY_ISSUE_42_SNAP, _PLAN_ISSUE_42_SNAP])
    async def test_comment_on_current_issue_bypasses_blocking_state(self, config, snap):
        mock_db = AsyncMock()
        with _online(snap=snap), _patch_send_message(True):
            result = await safe_deliver(
                "ike", "Comment", config, db=mock_db, delivery_kind="comment", **_issue_kwargs()
            )
        assert result == "delivered"

    async def test_comment_on_other_issue_is_queued_while_busy(self, config):
        mock_db = AsyncMock()
        with _online(snap=_BUSY_ISSUE_99_SNAP):
            result = await safe_deliver(
                "ike", "Comment", config, db=mock_db, delivery_kind="comment", **_issue_kwargs()
            )
        assert result == "agent_working"
        mock_db.enqueue_message.assert_called_once()
        assert mock_db.record_delivery.await_args.kwargs["outcome"] == "agent_working"
        assert mock_db.record_delivery.await_args.kwargs["kind"] == "comment"

    async def test_settling_defers(self, config):
        with patch(
            f"{_DELIV}.get_session_intelligence",
            new_callable=AsyncMock,
            return_value=SessionProfile("ike", SessionIntelligence.SETTLING),
        ):
            assert await safe_deliver("ike", "Hello", config) == "settling"

    async def test_settling_enqueues_non_issue_but_not_issue(self, config):
        mock_db = AsyncMock()
        with patch(
            f"{_DELIV}.get_session_intelligence",
            new_callable=AsyncMock,
            return_value=SessionProfile("ike", SessionIntelligence.SETTLING),
        ):
            comment = await safe_deliver(
                "ike", "Comment", config, db=mock_db, delivery_kind="comment", **_issue_kwargs()
            )
            mock_db.enqueue_message.assert_called_once()
            mock_db.enqueue_message.reset_mock()
            issue = await safe_deliver("ike", "Issue", config, db=mock_db, **_issue_kwargs())
            mock_db.enqueue_message.assert_not_called()
        assert comment == "settling" and issue == "settling"
