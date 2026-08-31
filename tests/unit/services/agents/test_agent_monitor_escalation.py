"""Tests for the agent monitor: escalation, plan-waiting notification, pending delivery."""

from __future__ import annotations

import time
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.config import EscalationConfig, TelegramConfig
from agent_backbone.models import IssueData, ParsedLabels
from agent_backbone.services.agents import AgentState, StateSnapshot
from agent_backbone.services.agents import _escalation as esc
from agent_backbone.services.agents._monitor import monitor_agents
from agent_backbone.services.agents._pending import deliver_pending_issues
from agent_backbone.services.database import BackboneDB

_ESC = "agent_backbone.services.agents._escalation"
_PEND = "agent_backbone.services.agents._pending"
_MON = "agent_backbone.services.agents._monitor"


@pytest.fixture(autouse=True)
def _clear_dedup():
    esc._escalation_dedup.clear()
    esc._plan_notify_dedup.clear()
    yield
    esc._escalation_dedup.clear()
    esc._plan_notify_dedup.clear()


@pytest.fixture
async def db():
    async with BackboneDB.connect() as db:
        yield db


def _snap(state: AgentState, issue: int | None = None, age: float = 0.0, **kwargs):
    return StateSnapshot(
        state=state, current_issue=issue, timestamp=time.time() - age, source="push", **kwargs
    )


def _patch_states(mapping: dict[str, StateSnapshot], module: str = _ESC):
    async def _get(state_path, session, stale):
        return mapping.get(session, StateSnapshot(state=AgentState.UNKNOWN))

    return patch(f"{module}.get_agent_state", side_effect=_get)


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
        esc._escalation_dedup[("ike", "stall:1")] = time.monotonic() - 100
        assert esc._should_escalate("ike", "stall:1", 60) is True


class TestCheckForStalls:
    async def test_detects_stall(self, config, db):
        states = {"ike": _snap(AgentState.BUSY, issue=42, age=6000, current_repo=_REPO)}
        with _patch_states(states):
            stalls = await esc.check_for_stalls(config, {"ike"}, db)
        assert stalls == [
            {
                "entity": "ike",
                "session": "ike",
                "issue_number": 42,
                "repo": _REPO,
                "duration_minutes": 100,
            }
        ]
        row = await db.get_agent_state("ike")
        assert row["state"] == "busy" and row["current_repo"] == _REPO

    @pytest.mark.parametrize(
        "snap",
        [
            _snap(AgentState.IDLE, age=6000),
            _snap(AgentState.BUSY, issue=None, age=6000),
            _snap(AgentState.BUSY, issue=1, age=10),
            _snap(_WAITING, issue=1, age=6000, reason="plan"),
        ],
    )
    async def test_not_stalled(self, config, db, snap):
        with _patch_states({"ike": snap}):
            assert await esc.check_for_stalls(config, {"ike"}, db) == []

    async def test_unconfigured_sessions_ignored(self, config, db):
        with _patch_states({"random": _snap(AgentState.BUSY, issue=1, age=6000)}):
            assert await esc.check_for_stalls(config, {"random"}, db) == []


class TestHandleStalls:
    async def test_escalates_to_target_once(self, config, db):
        config = replace(config, escalation=EscalationConfig(target="leo"))
        states = {"ike": _snap(AgentState.BUSY, issue=42, age=6000)}
        with _patch_states(states), patch(f"{_ESC}.safe_deliver", new_callable=AsyncMock) as d:
            await esc.handle_stalls(config, {"ike", "leo"}, db)
            await esc.handle_stalls(config, {"ike", "leo"}, db)
        d.assert_awaited_once()
        assert d.await_args.args[0] == "leo"
        assert "stalled" in d.await_args.args[1]
        assert d.await_args.kwargs["delivery_kind"] == "escalation"

    async def test_no_escalation_target(self, config, db):
        states = {"ike": _snap(AgentState.BUSY, issue=42, age=6000)}
        with _patch_states(states), patch(f"{_ESC}.safe_deliver", new_callable=AsyncMock) as d:
            await esc.handle_stalls(config, {"ike"}, db)
        d.assert_not_called()

    async def test_target_offline_skips_delivery(self, config, db):
        config = replace(config, escalation=EscalationConfig(target="leo"))
        states = {"ike": _snap(AgentState.BUSY, issue=42, age=6000)}
        with _patch_states(states), patch(f"{_ESC}.safe_deliver", new_callable=AsyncMock) as d:
            await esc.handle_stalls(config, {"ike"}, db)
        d.assert_not_called()


class TestOffline:
    async def test_detects_and_clears_offline_agent(self, config, db):
        config = replace(config, escalation=EscalationConfig(target="leo"))
        await db.set_agent_state("ike", "busy", current_issue=3, entity="ike")
        gh = AsyncMock()
        gh.list_open_issues = AsyncMock(return_value=[_issue(3)])
        with patch(f"{_ESC}.safe_deliver", new_callable=AsyncMock) as d:
            await esc.handle_offline(config, {"leo"}, db, gh)
        d.assert_awaited_once()
        assert "offline unexpectedly" in d.await_args.args[1]
        assert "1 pending issue" in d.await_args.args[1]
        assert (await db.get_agent_state("ike"))["state"] == "unknown"

    async def test_active_or_unknown_not_flagged(self, config, db):
        await db.set_agent_state("ike", "busy", entity="ike")
        await db.set_agent_state("leo", "unknown", entity="leo")
        assert await esc.check_for_unexpected_offline(config, {"ike"}, db, AsyncMock()) == []


class TestPlanWaiting:
    async def test_notifies_telegram_and_target_once(self, config, db):
        config = replace(
            config,
            telegram_token="tok",
            telegram=TelegramConfig(notification_chat_id=5),
            escalation=EscalationConfig(target="leo"),
        )
        states = {"ike": _snap(_WAITING, reason="plan", plan_file="/p.md", plan_title="T")}
        with (
            _patch_states(states),
            patch(
                f"{_ESC}.TelegramService.send_notification",
                new_callable=AsyncMock,
                return_value=True,
            ) as tg,
            patch(f"{_ESC}.safe_deliver", new_callable=AsyncMock, return_value="delivered") as d,
        ):
            await esc.check_plan_waiting(config, {"ike", "leo"}, db=db)
            await esc.check_plan_waiting(config, {"ike", "leo"}, db=db)

        tg.assert_awaited_once()
        assert "/approve ike" in tg.await_args.args[2]
        d.assert_awaited_once()
        assert d.await_args.args[0] == "leo"
        assert "created a plan" in d.await_args.args[1]

    async def test_new_plan_timestamp_renotifies(self, config, db):
        config = replace(
            config, telegram_token="tok", telegram=TelegramConfig(notification_chat_id=5)
        )
        first = _snap(_WAITING, reason="plan", plan_file="/p.md", plan_title="T")
        second = replace(first, timestamp=first.timestamp + 10)
        with patch(
            f"{_ESC}.TelegramService.send_notification", new_callable=AsyncMock, return_value=True
        ) as tg:
            with _patch_states({"ike": first}):
                await esc.check_plan_waiting(config, {"ike"}, db=db)
            with _patch_states({"ike": second}):
                await esc.check_plan_waiting(config, {"ike"}, db=db)
        assert tg.await_count == 2

    async def test_nothing_without_telegram_or_target(self, config, db):
        states = {"ike": _snap(_WAITING, reason="plan")}
        with (
            _patch_states(states),
            patch(f"{_ESC}.TelegramService.send_notification", new_callable=AsyncMock) as tg,
            patch(f"{_ESC}.safe_deliver", new_callable=AsyncMock) as d,
        ):
            await esc.check_plan_waiting(config, {"ike"}, db=db)
        tg.assert_not_called()
        d.assert_not_called()


class TestDeliverPendingIssues:
    async def test_delivers_first_pending_to_idle_agent(self, config, db):
        gh = AsyncMock()
        gh.list_open_issues = AsyncMock(return_value=[_issue(7), _issue(8)])
        gh.list_comments = AsyncMock(return_value=[])
        with (
            _patch_states({"ike": _snap(AgentState.IDLE)}, _PEND),
            patch(f"{_PEND}.safe_deliver", new_callable=AsyncMock, return_value="delivered") as d,
        ):
            result = await deliver_pending_issues(config, {"ike"}, db, gh)

        assert result["ike"] == "delivered_#7"
        assert d.await_args.kwargs["queue_scope"] == {(_REPO, 7), (_REPO, 8)}
        assert d.await_args.kwargs["repo"] == _REPO
        assert d.await_args.kwargs["enforce_issue_queue"] is True

    async def test_defers_busy_agent(self, config, db):
        gh = AsyncMock()
        with (
            _patch_states({"ike": _snap(AgentState.BUSY)}, _PEND),
            patch(f"{_PEND}.safe_deliver", new_callable=AsyncMock) as d,
        ):
            result = await deliver_pending_issues(config, {"ike"}, db, gh)
        assert result["ike"] == "deferred"
        d.assert_not_called()

    async def test_skips_acknowledged_issue(self, config, db):
        await db.record_acknowledgment(7, "ike", repo=_REPO)
        gh = AsyncMock()
        gh.list_open_issues = AsyncMock(return_value=[_issue(7), _issue(8)])
        gh.list_comments = AsyncMock(return_value=[])
        with (
            _patch_states({"ike": _snap(AgentState.IDLE)}, _PEND),
            patch(f"{_PEND}.safe_deliver", new_callable=AsyncMock, return_value="delivered") as d,
        ):
            result = await deliver_pending_issues(config, {"ike"}, db, gh)
        assert result["ike"] == "delivered_#8"
        assert d.await_args.kwargs["issue_number"] == 8

    async def test_backfills_ack_from_github_comments(self, config, db):
        from agent_backbone.models import CommentData

        gh = AsyncMock()
        gh.list_open_issues = AsyncMock(return_value=[_issue(7)])
        gh.list_comments = AsyncMock(
            return_value=[CommentData(body="[from:ike] on it", user_login="bot")]
        )
        with (
            _patch_states({"ike": _snap(AgentState.IDLE)}, _PEND),
            patch(f"{_PEND}.safe_deliver", new_callable=AsyncMock) as d,
        ):
            result = await deliver_pending_issues(config, {"ike"}, db, gh)
        assert result["ike"] == "no_deliverable"
        d.assert_not_called()
        assert await db.is_acknowledged(7, "ike", repo=_REPO)

    async def test_skips_recently_delivered(self, config, db):
        await db.record_delivery(7, "ike", "ike", "delivered", "agent-monitor", repo=_REPO)
        gh = AsyncMock()
        gh.list_open_issues = AsyncMock(return_value=[_issue(7)])
        gh.list_comments = AsyncMock(return_value=[])
        with (
            _patch_states({"ike": _snap(AgentState.IDLE)}, _PEND),
            patch(f"{_PEND}.safe_deliver", new_callable=AsyncMock) as d,
        ):
            result = await deliver_pending_issues(config, {"ike"}, db, gh)
        assert result["ike"] == "recently_delivered"
        d.assert_not_called()

    async def test_no_pending(self, config, db):
        gh = AsyncMock()
        gh.list_open_issues = AsyncMock(return_value=[])
        with _patch_states({"ike": _snap(AgentState.IDLE)}, _PEND):
            result = await deliver_pending_issues(config, {"ike"}, db, gh)
        assert result["ike"] == "no_pending"


class TestMonitorAgents:
    async def test_runs_all_steps(self, config, db):
        gh = AsyncMock()
        with (
            patch(f"{_MON}.list_sessions", new_callable=AsyncMock, return_value=["ike"]),
            patch(f"{_MON}.sync_dependencies", new_callable=AsyncMock) as sync,
            patch(f"{_MON}.handle_stalls", new_callable=AsyncMock) as stalls,
            patch(f"{_MON}.handle_offline", new_callable=AsyncMock) as offline,
            patch(f"{_MON}.check_plan_waiting", new_callable=AsyncMock) as plans,
            patch(f"{_MON}.handle_copy_mode_recovery", new_callable=AsyncMock) as copy,
            patch(f"{_MON}.emit_sessions_update", new_callable=AsyncMock) as emit,
            patch(f"{_MON}.drain_message_queue", new_callable=AsyncMock, return_value={}) as drain,
            patch(
                f"{_MON}.deliver_pending_issues",
                new_callable=AsyncMock,
                return_value={"ike": "no_pending"},
            ) as pend,
        ):
            result = await monitor_agents(config, db, gh)

        assert result == {"ike": "no_pending"}
        for mock in (sync, stalls, offline, plans, copy, emit, drain, pend):
            mock.assert_awaited_once()

    async def test_step_failure_is_isolated(self, config, db):
        with (
            patch(f"{_MON}.list_sessions", new_callable=AsyncMock, return_value=["ike"]),
            patch(
                f"{_MON}.sync_dependencies", new_callable=AsyncMock, side_effect=RuntimeError("x")
            ),
            patch(f"{_MON}.handle_stalls", new_callable=AsyncMock, side_effect=RuntimeError("x")),
            patch(f"{_MON}.handle_offline", new_callable=AsyncMock),
            patch(f"{_MON}.check_plan_waiting", new_callable=AsyncMock),
            patch(f"{_MON}.handle_copy_mode_recovery", new_callable=AsyncMock),
            patch(f"{_MON}.emit_sessions_update", new_callable=AsyncMock),
            patch(f"{_MON}.drain_message_queue", new_callable=AsyncMock, return_value={}),
            patch(
                f"{_MON}.deliver_pending_issues", new_callable=AsyncMock, return_value={}
            ) as pend,
        ):
            await monitor_agents(config, db, AsyncMock())
        pend.assert_awaited_once()

    async def test_without_github_skips_issue_delivery(self, config, db):
        with (
            patch(f"{_MON}.list_sessions", new_callable=AsyncMock, return_value=["ike"]),
            patch(f"{_MON}.handle_stalls", new_callable=AsyncMock),
            patch(f"{_MON}.handle_offline", new_callable=AsyncMock),
            patch(f"{_MON}.check_plan_waiting", new_callable=AsyncMock),
            patch(f"{_MON}.handle_copy_mode_recovery", new_callable=AsyncMock),
            patch(f"{_MON}.emit_sessions_update", new_callable=AsyncMock),
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
