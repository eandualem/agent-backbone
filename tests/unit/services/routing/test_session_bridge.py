"""Tests for session intelligence, target resolution and safe_deliver."""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.config import TimingConfig
from agent_backbone.services.agents import AgentState, StateSnapshot
from agent_backbone.services.routing import deliver, get_session_intelligence, safe_deliver
from agent_backbone.services.routing._resolution import (
    resolve_entity_session,
    validate_issue_targets,
)
from agent_backbone.services.routing.models import SessionIntelligence, SessionProfile

_IDLE_SNAP = StateSnapshot(state=AgentState.IDLE, source="push")
_BUSY_SNAP = StateSnapshot(state=AgentState.BUSY, source="push")
_STARTING_SNAP = StateSnapshot(state=AgentState.STARTING, source="push")
_BLOCKED_SNAP = StateSnapshot(
    state=AgentState.BLOCKED,
    reason="quota",
    detail="resets at 3 PM",
    current_issue=42,
    source="push",
)
_PLAN_SNAP = StateSnapshot(state=AgentState.WAITING_FOR_HUMAN, reason="plan", source="push")
_PERMISSION_SNAP = StateSnapshot(
    state=AgentState.WAITING_FOR_HUMAN, reason="permission", source="push"
)
_UNKNOWN_SNAP = StateSnapshot(state=AgentState.UNKNOWN, source="default")
_REPO = "example/orchestration"
_BUSY_ISSUE_42_SNAP = StateSnapshot(
    state=AgentState.BUSY, current_issue=42, current_repo=_REPO, source="push"
)
_BUSY_ISSUE_99_SNAP = StateSnapshot(
    state=AgentState.BUSY, current_issue=99, current_repo=_REPO, source="push"
)
_PLAN_ISSUE_42_SNAP = StateSnapshot(
    state=AgentState.WAITING_FOR_HUMAN,
    reason="plan",
    current_issue=42,
    current_repo=_REPO,
    source="push",
)
_BUSY_ISSUE_42_UNKNOWN_REPO_SNAP = StateSnapshot(
    state=AgentState.BUSY, current_issue=42, source="push"
)

_INTEL = "agent_backbone.services.routing._intelligence"
_DELIV = "agent_backbone.services.routing._delivery"
_COPY = "agent_backbone.services.terminal._copy_mode"


class TestDeliverySerialization:
    async def test_same_session_gate_waits_for_recording_but_other_sessions_continue(
        self, config, db
    ):
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(session, message, **kwargs):
            if message == "first":
                entered.set()
                await release.wait()
            return True

        profile = SessionProfile(session_name="ike", intelligence=SessionIntelligence.READY)
        with (
            patch(f"{_DELIV}.get_session_intelligence", AsyncMock(return_value=profile)),
            patch(f"{_DELIV}.send_message", AsyncMock(side_effect=send)) as paste,
        ):
            first = asyncio.create_task(
                deliver(
                    "ike",
                    "first",
                    config,
                    db=db,
                    repo=_REPO,
                    issue_number=1,
                    target_entity="ike",
                    enforce_issue_queue=True,
                )
            )
            await entered.wait()
            second = asyncio.create_task(
                deliver(
                    "ike",
                    "second",
                    config,
                    db=db,
                    repo=_REPO,
                    issue_number=2,
                    target_entity="ike",
                    enforce_issue_queue=True,
                )
            )
            try:
                other = await asyncio.wait_for(
                    deliver("leo", "other", config, delivery_kind="direct_message"), timeout=1
                )
                assert other.outcome == "delivered"
                assert not second.done()
            finally:
                release.set()
                results = await asyncio.gather(first, second)
        assert [result.outcome for result in results] == ["delivered", "awaiting_ack"]
        assert [call.args[1] for call in paste.await_args_list] == ["first", "other"]

    async def test_cancelling_waiter_and_holder_leaves_session_usable(self, config):
        entered = asyncio.Event()

        async def send(session, message, **kwargs):
            if message == "first":
                entered.set()
                await asyncio.Event().wait()
            return True

        profile = SessionProfile(session_name="ike", intelligence=SessionIntelligence.READY)
        with (
            patch(f"{_DELIV}.get_session_intelligence", AsyncMock(return_value=profile)),
            patch(f"{_DELIV}.send_message", AsyncMock(side_effect=send)),
        ):
            first = asyncio.create_task(
                deliver("ike", "first", config, delivery_kind="direct_message")
            )
            await entered.wait()
            waiter = asyncio.create_task(
                deliver("ike", "waiting", config, delivery_kind="direct_message")
            )
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            result = await asyncio.wait_for(
                deliver("ike", "after cancellation", config, delivery_kind="direct_message"),
                timeout=1,
            )
        assert result.outcome == "delivered"

    async def test_finished_deliveries_do_not_retain_session_locks(self, config):
        from agent_backbone.services.routing._delivery import _session_locks

        profile = SessionProfile(session_name="unused", intelligence=SessionIntelligence.OFFLINE)
        with patch(f"{_DELIV}.get_session_intelligence", AsyncMock(return_value=profile)):
            for number in range(20):
                name = f"temporary-{number}"
                await deliver(name, "hello", config, delivery_kind="direct_message")
                assert name not in _session_locks


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
            (_BLOCKED_SNAP, SessionIntelligence.AGENT_WORKING),
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
        config = replace(config, timing=TimingConfig(grace_period_seconds=5))
        with _online():
            profile = await get_session_intelligence("ike", config, idle_since=time.monotonic() - 1)
        assert profile.intelligence == SessionIntelligence.SETTLING

    async def test_settling_elapsed(self, config):
        config = replace(config, timing=TimingConfig(grace_period_seconds=1))
        with _online():
            profile = await get_session_intelligence(
                "ike", config, idle_since=time.monotonic() - 10
            )
        assert profile.intelligence == SessionIntelligence.READY

    async def test_hook_idle_timestamp_starts_grace(self, config):
        # A freshly hook-reported idle (wall-clock transition time) settles;
        # no explicit idle_since needed — the push timestamp is the source.
        from agent_backbone.services.agents import StateSnapshot as Snap

        config = replace(config, timing=TimingConfig(grace_period_seconds=60))
        snap = Snap(state=AgentState.IDLE, source="push", timestamp=time.time() - 1)
        with _online(snap=snap):
            profile = await get_session_intelligence("ike", config)
        assert profile.intelligence == SessionIntelligence.SETTLING

    async def test_hook_idle_beyond_grace_is_ready(self, config):
        from agent_backbone.services.agents import StateSnapshot as Snap

        config = replace(config, timing=TimingConfig(grace_period_seconds=60))
        snap = Snap(state=AgentState.IDLE, source="push", timestamp=time.time() - 3600)
        with _online(snap=snap):
            profile = await get_session_intelligence("ike", config)
        assert profile.intelligence == SessionIntelligence.READY

    async def test_terminal_idle_carries_no_grace(self, config):
        # A terminal reading is stamped at read time: deriving grace from it
        # would reset the window on every read and settle forever.
        from agent_backbone.services.agents import StateSnapshot as Snap

        config = replace(config, timing=TimingConfig(grace_period_seconds=3600))
        snap = Snap(state=AgentState.IDLE, source="pull", timestamp=time.time())
        with _online(snap=snap):
            profile = await get_session_intelligence("ike", config)
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
        mock_db.queue.enqueue.assert_called_once_with(
            session_name="ike",
            message="Hello",
            issue_number=42,
            target_entity="ike",
            delivery_kind="issue",
            source="t",
            repo="example/orchestration",
            sender="",
            source_key=None,
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
        mock_db.deliveries.record.assert_called_once_with(
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
        mock_db.queue.enqueue.assert_called_once()

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
        mock_db.queue.enqueue.assert_called_once_with(
            session_name="ike",
            message="Hello",
            issue_number=None,
            target_entity=None,
            delivery_kind="direct_message",
            source="api-messages",
            repo="",
            sender="",
            source_key=None,
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
        mock_db.queue.enqueue.assert_called_once()

    async def test_unknown_state_still_delivers(self, config):
        mock_db = AsyncMock()
        with _online(snap=_UNKNOWN_SNAP), _patch_send_message(True):
            result = await safe_deliver("ike", "Hello", config, db=mock_db, **_issue_kwargs())
        assert result == "delivered"
        mock_db.queue.enqueue.assert_not_called()
        mock_db.deliveries.finalize.assert_called_once()

    async def test_enforce_issue_queue_blocks_duplicate_issue(self, config):
        mock_db = AsyncMock()
        mock_db.deliveries.query.return_value = [
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
        mock_db.deliveries.record.assert_not_called()

    async def test_enforce_issue_queue_blocks_until_acknowledged(self, config):
        mock_db = AsyncMock()
        mock_db.deliveries.query.side_effect = [
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
        mock_db.acks.exists.return_value = False
        result = await safe_deliver(
            "ike", "Hello", config, db=mock_db, enforce_issue_queue=True, **_issue_kwargs()
        )
        assert result == "awaiting_ack"
        mock_db.deliveries.record.assert_not_called()

    async def test_same_number_in_other_repo_does_not_block(self, config):
        mock_db = AsyncMock()
        mock_db.deliveries.query.side_effect = [
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
        mock_db.acks.exists.return_value = False
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
        mock_db.deliveries.query.return_value = []
        order: list[str] = []

        async def _claim(*args, **kwargs):
            order.append("claim")
            return 123

        async def _send(*args, **kwargs):
            order.append("send")
            return True

        async def _finalize(*args, **kwargs):
            order.append("finalize")

        mock_db.deliveries.claim = AsyncMock(side_effect=_claim)
        mock_db.deliveries.finalize = AsyncMock(side_effect=_finalize)
        with _online(), patch(f"{_DELIV}.send_message", new_callable=AsyncMock, side_effect=_send):
            result = await safe_deliver("ike", "Hello", config, db=mock_db, **_issue_kwargs())
        assert result == "delivered"
        assert order == ["claim", "send", "finalize"]
        mock_db.deliveries.finalize.assert_awaited_once_with(123, "delivered")
        assert mock_db.deliveries.claim.await_args.kwargs["repo"] == "example/orchestration"
        mock_db.deliveries.record.assert_not_called()

    async def test_safe_deliver_claim_conflict_returns_already_delivered(self, config):
        mock_db = AsyncMock()
        mock_db.deliveries.query.return_value = []
        mock_db.deliveries.claim.return_value = None
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
        mock_db.deliveries.record.assert_called_once_with(
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

    async def test_comment_on_same_number_in_unknown_repo_does_not_bypass(self, config):
        # other/repo#42 must not slip past busy protection because the agent
        # works on #42 of a repository the hook did not name.
        mock_db = AsyncMock()
        with _online(snap=_BUSY_ISSUE_42_UNKNOWN_REPO_SNAP):
            result = await safe_deliver(
                "ike", "Comment", config, db=mock_db, delivery_kind="comment", **_issue_kwargs()
            )
        assert result == "agent_working"

    async def test_comment_on_other_issue_is_queued_while_busy(self, config):
        mock_db = AsyncMock()
        with _online(snap=_BUSY_ISSUE_99_SNAP):
            result = await safe_deliver(
                "ike", "Comment", config, db=mock_db, delivery_kind="comment", **_issue_kwargs()
            )
        assert result == "agent_working"
        mock_db.queue.enqueue.assert_called_once()
        assert mock_db.deliveries.record.await_args.kwargs["outcome"] == "agent_working"
        assert mock_db.deliveries.record.await_args.kwargs["kind"] == "comment"

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
            mock_db.queue.enqueue.assert_called_once()
            mock_db.queue.enqueue.reset_mock()
            issue = await safe_deliver("ike", "Issue", config, db=mock_db, **_issue_kwargs())
            mock_db.queue.enqueue.assert_not_called()
        assert comment == "settling" and issue == "settling"


class TestPlanResponseDelivery:
    """A plan response goes in exactly when the agent waits for a plan decision."""

    async def test_delivered_while_waiting_for_a_plan(self, config):
        mock_db = AsyncMock()
        with _online(snap=_PLAN_SNAP), _patch_send_message(True) as send:
            result = await safe_deliver(
                "ike", "2", config, db=mock_db, source="api-plans", delivery_kind="plan_response"
            )
        assert result == "delivered"
        send.assert_called_once_with("ike", "2", runtime_hint="unknown")
        assert mock_db.deliveries.record.await_args.kwargs["kind"] == "plan_response"
        mock_db.queue.enqueue.assert_not_called()

    async def test_a_permission_prompt_is_not_a_plan(self, config):
        mock_db = AsyncMock()
        with _online(snap=_PERMISSION_SNAP), _patch_send_message(True) as send:
            result = await safe_deliver(
                "ike", "2", config, db=mock_db, delivery_kind="plan_response"
            )
        assert result == "not_waiting"
        send.assert_not_called()
        mock_db.queue.enqueue.assert_not_called()

    async def test_anything_but_a_waiting_plan_is_not_waiting_and_never_queued(self, config):
        # An idle prompt is the dangerous case: a bare "2" would become an instruction.
        for snap in (_IDLE_SNAP, _BUSY_SNAP, _UNKNOWN_SNAP):
            mock_db = AsyncMock()
            with _online(snap=snap), _patch_send_message(True) as send:
                result = await safe_deliver(
                    "ike", "2", config, db=mock_db, delivery_kind="plan_response"
                )
            assert result == "not_waiting", snap
            send.assert_not_called()
            mock_db.queue.enqueue.assert_not_called()

    async def test_offline_agent_is_offline_not_queued(self, config):
        mock_db = AsyncMock()
        with _patch_list_sessions([]):
            result = await safe_deliver(
                "ike", "2", config, db=mock_db, delivery_kind="plan_response"
            )
        assert result == "offline"
        mock_db.queue.enqueue.assert_not_called()


class TestDeliveryReport:
    """`queued` is claimed only for a message that is in the database."""

    def _db(self, status):
        from agent_backbone.services.database._queue_repo import EnqueueResult

        mock_db = AsyncMock()
        row_id = 1 if status == "inserted" else None
        mock_db.queue.enqueue.return_value = EnqueueResult(status, row_id)
        return mock_db

    async def test_stored(self, config):
        mock_db = self._db("inserted")
        with _online(snap=_BUSY_SNAP):
            report = await deliver(
                "ike", "hi", config, db=mock_db, delivery_kind="direct_message", sender="leo"
            )
        assert report.outcome == "agent_working"
        assert report.queue == "stored" and report.queued
        assert mock_db.queue.enqueue.await_args.kwargs["sender"] == "leo"

    async def test_already_queued_is_reported_as_such(self, config):
        mock_db = self._db("already_queued")
        with _online(snap=_BUSY_SNAP):
            report = await deliver("ike", "hi", config, db=mock_db, delivery_kind="direct_message")
        assert report.queue == "already_queued" and report.queued

    async def test_storage_error_is_failed_not_queued(self, config):
        mock_db = AsyncMock()
        mock_db.queue.enqueue.side_effect = RuntimeError("disk full")
        with _online(snap=_BUSY_SNAP):
            report = await deliver("ike", "hi", config, db=mock_db, delivery_kind="direct_message")
        assert report.outcome == "agent_working"
        assert report.queue == "failed" and not report.queued

    async def test_delivered_needs_no_queue(self, config):
        with _online(), _patch_send_message(True):
            report = await deliver("ike", "hi", config, delivery_kind="direct_message")
        assert report.outcome == "delivered" and report.queue is None and not report.queued


class TestBlockedAndOfflineMetadata:
    async def test_a_blocked_agent_keeps_its_issue_and_detail(self, config):
        with (
            patch(f"{_INTEL}.list_sessions", new_callable=AsyncMock, return_value=["ike"]),
            patch(f"{_INTEL}.capture_pane", new_callable=AsyncMock, return_value="❯ "),
            patch(f"{_INTEL}.get_agent_state", new_callable=AsyncMock, return_value=_BLOCKED_SNAP),
            patch(f"{_INTEL}.clear_copy_mode", new_callable=AsyncMock, return_value=(False, False)),
        ):
            profile = await get_session_intelligence("ike", config)
        assert profile.intelligence == SessionIntelligence.AGENT_WORKING
        assert profile.current_issue == 42 and profile.detail == "resets at 3 PM"

    async def test_an_offline_agent_still_shows_what_its_hook_recorded(self, config):
        from agent_backbone.services.agents import write_state_file

        write_state_file(
            config.state_dir,
            "ike",
            {"state": "unknown", "ts": 1.0, "session_id": "sess-1", "last_message": "bye"},
        )
        with patch(f"{_INTEL}.list_sessions", new_callable=AsyncMock, return_value=[]):
            profile = await get_session_intelligence("ike", config)
        assert profile.intelligence == SessionIntelligence.OFFLINE
        assert profile.session_id == "sess-1" and profile.last_message == "bye"
