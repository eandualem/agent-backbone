"""Tests for the agent monitor: escalation, plan-waiting notification, pending delivery."""

from __future__ import annotations

import time
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.config import EscalationConfig, TelegramConfig
from agent_backbone.models import DeliveryOutcome, IssueData, ParsedLabels
from agent_backbone.services.agents import AgentState, StateSnapshot
from agent_backbone.services.jobs import escalation as esc
from agent_backbone.services.jobs.monitor import monitor_agents, read_states, sync_states
from agent_backbone.services.jobs.pending import deliver_pending_issues

_ESC = "agent_backbone.services.jobs.escalation"
_PEND = "agent_backbone.services.jobs.pending"
_MON = "agent_backbone.services.jobs.monitor"


@pytest.fixture(autouse=True)
def _clear_dedup():
    esc._escalated.clear()
    esc._plan_notified.clear()
    yield
    esc._escalated.clear()
    esc._plan_notified.clear()


def _snap(state: AgentState, issue: int | None = None, age: float = 0.0, **kwargs):
    return StateSnapshot(
        state=state, current_issue=issue, timestamp=time.time() - age, source="push", **kwargs
    )


_REPO = "example/orchestration"
_WAITING = AgentState.WAITING_FOR_HUMAN


def _issue(number: int, target: str = "ike") -> IssueData:
    return IssueData(
        number=number,
        title=f"[task] #{number}",
        labels=ParsedLabels(sender="leo", targets=[target], issue_type="task"),
        repo_full_name=_REPO,
    )


class TestShouldEscalate:
    def test_first_allowed_then_suppressed(self):
        assert esc._should_escalate("ike", "stall:1", 60) is True
        assert esc._should_escalate("ike", "stall:1", 60) is False

    def test_different_key_allowed(self):
        esc._should_escalate("ike", "stall:1", 60)
        assert esc._should_escalate("ike", "stall:2", 60) is True
        assert esc._should_escalate("leo", "stall:1", 60) is True

    def test_expired_entry_re_allowed(self):
        esc._escalated._marked[("ike", "stall:1")] = time.monotonic() - 100
        assert esc._should_escalate("ike", "stall:1", 60) is True


class TestReadAndSyncStates:
    async def test_reads_only_configured_live_agents(self, config):
        async def _get(state_path, session, stale):
            return _snap(AgentState.BUSY, issue=1)

        with patch(f"{_MON}.get_agent_state", side_effect=_get) as get:
            states = await read_states(config, {"ike", "leo", "stranger"})
        assert set(states) == {"ike", "leo"}
        assert get.await_count == 2  # once per agent per tick, never more

    async def test_sync_mirrors_snapshots_into_the_database(self, db):
        await sync_states(db, {"ike": _snap(AgentState.BUSY, issue=42, current_repo=_REPO)})
        row = await db.get_agent_state("ike")
        assert row["state"] == "busy" and row["current_repo"] == _REPO


class TestCheckForStalls:
    async def test_detects_stall(self, config):
        states = {"ike": _snap(AgentState.BUSY, issue=42, age=6000, current_repo=_REPO)}
        assert await esc.check_for_stalls(config, states) == [
            {
                "entity": "ike",
                "session": "ike",
                "issue_number": 42,
                "repo": _REPO,
                "duration_minutes": 100,
            }
        ]

    @pytest.mark.parametrize(
        "snap",
        [
            _snap(AgentState.IDLE, age=6000),
            _snap(AgentState.BUSY, issue=None, age=6000),
            _snap(AgentState.BUSY, issue=1, age=10),
            _snap(_WAITING, issue=1, age=6000, reason="plan"),
        ],
    )
    async def test_not_stalled(self, config, snap):
        assert await esc.check_for_stalls(config, {"ike": snap}) == []


class TestHandleStalls:
    async def test_escalates_to_target_once(self, config, db):
        config = replace(config, escalation=EscalationConfig(target="leo"))
        states = {"ike": _snap(AgentState.BUSY, issue=42, age=6000), "leo": _snap(AgentState.IDLE)}
        with patch(f"{_ESC}.safe_deliver", new_callable=AsyncMock) as d:
            await esc.handle_stalls(config, states, db)
            await esc.handle_stalls(config, states, db)
        d.assert_awaited_once()
        assert d.await_args.args[0] == "leo"
        assert "stalled" in d.await_args.args[1]
        assert d.await_args.kwargs["delivery_kind"] == "escalation"

    async def test_no_escalation_target(self, config, db):
        states = {"ike": _snap(AgentState.BUSY, issue=42, age=6000)}
        with patch(f"{_ESC}.safe_deliver", new_callable=AsyncMock) as d:
            await esc.handle_stalls(config, states, db)
        d.assert_not_called()

    async def test_target_offline_skips_delivery(self, config, db):
        config = replace(config, escalation=EscalationConfig(target="leo"))
        states = {"ike": _snap(AgentState.BUSY, issue=42, age=6000)}  # leo has no session
        with patch(f"{_ESC}.safe_deliver", new_callable=AsyncMock) as d:
            await esc.handle_stalls(config, states, db)
        d.assert_not_called()


class TestOffline:
    async def test_detects_and_clears_offline_agent(self, config, db):
        config = replace(config, escalation=EscalationConfig(target="leo"))
        await db.set_agent_state("ike", "busy", current_issue=3)
        gh = AsyncMock()
        gh.list_issues = AsyncMock(return_value=[_issue(3)])
        with patch(f"{_ESC}.safe_deliver", new_callable=AsyncMock) as d:
            await esc.handle_offline(config, {"leo"}, db, gh)
        d.assert_awaited_once()
        assert "offline unexpectedly" in d.await_args.args[1]
        assert "1 pending issue" in d.await_args.args[1]
        assert (await db.get_agent_state("ike"))["state"] == "unknown"

    async def test_active_or_unknown_not_flagged(self, config, db):
        await db.set_agent_state("ike", "busy")
        await db.set_agent_state("leo", "unknown")
        assert await esc.check_for_unexpected_offline(config, {"ike"}, db, AsyncMock()) == []


class TestPlanWaiting:
    async def test_notifies_telegram_and_target_once(self, config, db):
        config = replace(
            config,
            telegram_token="tok",
            telegram=TelegramConfig(notification_chat_id=5),
            escalation=EscalationConfig(target="leo"),
        )
        states = {
            "ike": _snap(_WAITING, reason="plan", plan_file="/p.md", plan_title="T"),
            "leo": _snap(AgentState.IDLE),
        }
        with (
            patch(f"{_ESC}.notify_humans", new_callable=AsyncMock, return_value=True) as tg,
            patch(
                f"{_ESC}.safe_deliver",
                new_callable=AsyncMock,
                return_value=DeliveryOutcome.DELIVERED,
            ) as d,
        ):
            await esc.check_plan_waiting(config, states, db=db)
            await esc.check_plan_waiting(config, states, db=db)

        tg.assert_awaited_once()
        assert "/approve ike" in tg.await_args.args[1]
        assert tg.await_args.kwargs["agent"] == "ike"  # lands in the agent's own topic
        d.assert_awaited_once()
        assert d.await_args.args[0] == "leo"
        assert "created a plan" in d.await_args.args[1]

    async def test_new_plan_timestamp_renotifies(self, config, db):
        config = replace(
            config, telegram_token="tok", telegram=TelegramConfig(notification_chat_id=5)
        )
        first = _snap(_WAITING, reason="plan", plan_file="/p.md", plan_title="T")
        second = replace(first, timestamp=first.timestamp + 10)
        with patch(f"{_ESC}.notify_humans", new_callable=AsyncMock, return_value=True) as tg:
            await esc.check_plan_waiting(config, {"ike": first}, db=db)
            await esc.check_plan_waiting(config, {"ike": second}, db=db)
        assert tg.await_count == 2

    async def test_nothing_without_integration_or_target(self, config, db):
        # No integration configured: notify_humans reports False (so the
        # notification is not recorded as sent) and no escalation target exists.
        states = {"ike": _snap(_WAITING, reason="plan")}
        with (
            patch(f"{_ESC}.notify_humans", new_callable=AsyncMock, return_value=False) as tg,
            patch(f"{_ESC}.safe_deliver", new_callable=AsyncMock) as d,
        ):
            await esc.check_plan_waiting(config, states, db=db)
            await esc.check_plan_waiting(config, states, db=db)
        assert tg.await_count == 2  # not recorded as sent, so tried again next tick
        d.assert_not_called()

    async def test_real_notify_humans_is_false_when_nothing_is_configured(self, config, db):
        states = {"ike": _snap(_WAITING, reason="plan")}
        with (
            patch(f"{_ESC}.safe_deliver", new_callable=AsyncMock) as d,
            patch(f"{_ESC}._record_plan_notification") as recorded,
        ):
            await esc.check_plan_waiting(config, states, db=db)
        d.assert_not_called()
        recorded.assert_not_called()  # the real notify_humans returned False: nothing "sent"


_IDLE = {"ike": _snap(AgentState.IDLE)}


class TestDeliverPendingIssues:
    async def test_delivers_first_pending_to_idle_agent(self, config, db):
        gh = AsyncMock()
        gh.list_issues = AsyncMock(return_value=[_issue(7), _issue(8)])
        gh.list_comments = AsyncMock(return_value=[])
        with (
            patch(
                f"{_PEND}.safe_deliver",
                new_callable=AsyncMock,
                return_value=DeliveryOutcome.DELIVERED,
            ) as d,
        ):
            result = await deliver_pending_issues(config, _IDLE, db, gh)

        assert result["ike"] == "delivered_#7"
        assert d.await_args.kwargs["queue_scope"] == {(_REPO, 7), (_REPO, 8)}
        assert d.await_args.kwargs["repo"] == _REPO
        assert d.await_args.kwargs["enforce_issue_queue"] is True

    async def test_defers_busy_agent(self, config, db):
        gh = AsyncMock()
        with patch(f"{_PEND}.safe_deliver", new_callable=AsyncMock) as d:
            result = await deliver_pending_issues(config, {"ike": _snap(AgentState.BUSY)}, db, gh)
        assert result["ike"] == "deferred"
        d.assert_not_called()

    async def test_skips_acknowledged_issue(self, config, db):
        await db.record_acknowledgment(7, "ike", repo=_REPO)
        gh = AsyncMock()
        gh.list_issues = AsyncMock(return_value=[_issue(7), _issue(8)])
        gh.list_comments = AsyncMock(return_value=[])
        with (
            patch(
                f"{_PEND}.safe_deliver",
                new_callable=AsyncMock,
                return_value=DeliveryOutcome.DELIVERED,
            ) as d,
        ):
            result = await deliver_pending_issues(config, _IDLE, db, gh)
        assert result["ike"] == "delivered_#8"
        assert d.await_args.kwargs["issue_number"] == 8

    async def test_backfills_ack_from_github_comments(self, config, db):
        from agent_backbone.models import CommentData

        gh = AsyncMock()
        gh.list_issues = AsyncMock(return_value=[_issue(7)])
        gh.list_comments = AsyncMock(
            return_value=[CommentData(body="[from:ike] on it", user_login="bot")]
        )
        with (
            patch(f"{_PEND}.safe_deliver", new_callable=AsyncMock) as d,
        ):
            result = await deliver_pending_issues(config, _IDLE, db, gh)
        assert result["ike"] == "no_deliverable"
        d.assert_not_called()
        assert await db.is_acknowledged(7, "ike", repo=_REPO)

    async def test_skips_recently_delivered(self, config, db):
        await db.record_delivery(
            issue_number=7,
            target_entity="ike",
            session_name="ike",
            outcome="delivered",
            source="agent-monitor",
            repo=_REPO,
        )
        gh = AsyncMock()
        gh.list_issues = AsyncMock(return_value=[_issue(7)])
        gh.list_comments = AsyncMock(return_value=[])
        with (
            patch(f"{_PEND}.safe_deliver", new_callable=AsyncMock) as d,
        ):
            result = await deliver_pending_issues(config, _IDLE, db, gh)
        assert result["ike"] == "recently_delivered"
        d.assert_not_called()

    async def test_no_pending(self, config, db):
        gh = AsyncMock()
        gh.list_issues = AsyncMock(return_value=[])
        result = await deliver_pending_issues(config, _IDLE, db, gh)
        assert result["ike"] == "no_pending"


class TestMonitorAgents:
    async def test_runs_all_steps(self, config, db):
        gh = AsyncMock()
        with (
            patch(f"{_MON}.list_sessions", new_callable=AsyncMock, return_value=["ike"]),
            patch(
                f"{_MON}.get_agent_state",
                new_callable=AsyncMock,
                return_value=_snap(AgentState.IDLE),
            ),
            patch(f"{_MON}.sync_dependencies", new_callable=AsyncMock) as sync,
            patch(f"{_MON}.handle_stalls", new_callable=AsyncMock) as stalls,
            patch(f"{_MON}.handle_offline", new_callable=AsyncMock) as offline,
            patch(f"{_MON}.check_plan_waiting", new_callable=AsyncMock) as plans,
            patch(f"{_MON}.handle_copy_mode_recovery", new_callable=AsyncMock) as copy,
            patch(f"{_MON}.drain_message_queue", new_callable=AsyncMock, return_value={}) as drain,
            patch(
                f"{_MON}.deliver_pending_issues",
                new_callable=AsyncMock,
                return_value={"ike": "no_pending"},
            ) as pend,
        ):
            on_change = AsyncMock()
            result = await monitor_agents(config, db, gh, on_change=on_change)

        assert result == {"ike": "no_pending"}
        for mock in (sync, stalls, offline, plans, copy, on_change, drain, pend):
            mock.assert_awaited_once()

    async def test_step_failure_is_isolated(self, config, db):
        with (
            patch(f"{_MON}.list_sessions", new_callable=AsyncMock, return_value=["ike"]),
            patch(
                f"{_MON}.get_agent_state",
                new_callable=AsyncMock,
                return_value=_snap(AgentState.IDLE),
            ),
            patch(
                f"{_MON}.sync_dependencies", new_callable=AsyncMock, side_effect=RuntimeError("x")
            ),
            patch(f"{_MON}.handle_stalls", new_callable=AsyncMock, side_effect=RuntimeError("x")),
            patch(f"{_MON}.handle_offline", new_callable=AsyncMock),
            patch(f"{_MON}.check_plan_waiting", new_callable=AsyncMock),
            patch(f"{_MON}.handle_copy_mode_recovery", new_callable=AsyncMock),
            patch(f"{_MON}.drain_message_queue", new_callable=AsyncMock, return_value={}),
            patch(
                f"{_MON}.deliver_pending_issues", new_callable=AsyncMock, return_value={}
            ) as pend,
        ):
            await monitor_agents(
                config, db, AsyncMock(), on_change=AsyncMock(side_effect=RuntimeError("x"))
            )
        pend.assert_awaited_once()

    async def test_without_github_skips_issue_delivery(self, config, db):
        with (
            patch(f"{_MON}.list_sessions", new_callable=AsyncMock, return_value=["ike"]),
            patch(
                f"{_MON}.get_agent_state",
                new_callable=AsyncMock,
                return_value=_snap(AgentState.IDLE),
            ),
            patch(f"{_MON}.handle_stalls", new_callable=AsyncMock),
            patch(f"{_MON}.handle_offline", new_callable=AsyncMock),
            patch(f"{_MON}.check_plan_waiting", new_callable=AsyncMock),
            patch(f"{_MON}.handle_copy_mode_recovery", new_callable=AsyncMock),
            patch(f"{_MON}.drain_message_queue", new_callable=AsyncMock, return_value={}) as drain,
            patch(f"{_MON}.deliver_pending_issues", new_callable=AsyncMock) as pend,
        ):
            result = await monitor_agents(config, db, None)
        assert result == {}
        drain.assert_awaited_once()
        pend.assert_not_called()

    async def test_no_sessions_short_circuits(self, config, db):
        with patch(f"{_MON}.list_sessions", new_callable=AsyncMock, return_value=[]):
            assert await monitor_agents(config, db, AsyncMock()) == {}
