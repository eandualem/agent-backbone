"""Tests for DB-backed agent state and terminal-derived state helpers."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from agent_backbone.services.agents import (
    AgentState,
    find_outgoing_comment,
    has_commented_on_issue,
    should_deliver,
)
from agent_backbone.services.agents.interface import StateService, _row_to_snapshot
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.terminal import infer_state_from_pane, prompt_has_pending_input
from agent_backbone.services.terminal._adapters import TerminalRuntime, get_terminal_adapter

_IFACE = "agent_backbone.services.agents.interface"


class TestPlanWaiting:
    def test_plan_waiting_enum_value(self):
        assert AgentState.PLAN_WAITING == "plan_waiting"
        assert AgentState("plan_waiting") == AgentState.PLAN_WAITING

    def test_should_deliver_plan_waiting_default(self):
        assert should_deliver(AgentState.PLAN_WAITING) is False

    def test_permission_waiting_enum_value(self):
        assert AgentState.PERMISSION_WAITING == "permission_waiting"
        assert AgentState("permission_waiting") == AgentState.PERMISSION_WAITING

    def test_should_deliver_permission_waiting_default(self):
        assert should_deliver(AgentState.PERMISSION_WAITING) is False


class TestInferStateFromPane:
    def test_empty_content_is_offline(self):
        result = infer_state_from_pane("")
        assert result.state == AgentState.OFFLINE

    def test_idle_prompt(self):
        result = infer_state_from_pane("user@host ~/project $")
        assert result.state == AgentState.IDLE

    def test_idle_zsh_prompt(self):
        result = infer_state_from_pane("~/project %")
        assert result.state == AgentState.IDLE

    def test_busy_thinking(self):
        result = infer_state_from_pane("some output\nThinking...\nmore stuff")
        assert result.state == AgentState.BUSY

    def test_busy_tool_call(self):
        result = infer_state_from_pane("Running tool call: Read\nfile.py")
        assert result.state == AgentState.BUSY

    def test_processing_issue(self):
        content = "Starting task\nWorking on issue #42\nmore output"
        result = infer_state_from_pane(content)
        assert result.state == AgentState.BUSY
        assert result.current_issue == 42

    def test_unclassified_content_is_offline(self):
        result = infer_state_from_pane("random output with no indicators")
        assert result.state == AgentState.OFFLINE

    def test_source_is_pull(self):
        result = infer_state_from_pane("user@host $")
        assert result.source == "pull"

    def test_idle_claude_code_prompt(self):
        result = infer_state_from_pane("\u276f ")
        assert result.state == AgentState.IDLE

    def test_idle_claude_code_with_status_bar(self):
        sep = "\u2500" * 11
        pane = f"\u276f \n{sep}\n  12 files +177 -1242"
        result = infer_state_from_pane(pane)
        assert result.state == AgentState.IDLE

    def test_idle_prompt_above_separator(self):
        sep = "\u2500" * 7
        pane = f"{sep}\n\u276f \n{sep}\n  stats"
        result = infer_state_from_pane(pane)
        assert result.state == AgentState.IDLE

    def test_idle_claude_code_prompt_with_ansi_formatting(self):
        pane = "\x1b[39m\u276f\xa0\x1b[7m \x1b[0m"
        result = infer_state_from_pane(pane)
        assert result.state == AgentState.IDLE

    def test_idle_codex_prompt_marker(self):
        result = infer_state_from_pane("\u203a ")
        assert result.state == AgentState.IDLE

    def test_idle_codex_prompt_with_buffered_input(self):
        pane = "\u203a Improve documentation in @filename"
        result = infer_state_from_pane(pane)
        assert result.state == AgentState.IDLE

    def test_idle_codex_prompt_with_status_line_below(self):
        pane = (
            "\u203a Improve documentation in @filename\n\n"
            "  gpt-5.4 xhigh \u00b7 91% left \u00b7 ~/ws/core/code/WF/agent-orchestration-dashboard"
        )
        result = infer_state_from_pane(pane)
        assert result.state == AgentState.IDLE

    def test_idle_codex_prompt_ignores_stale_runtime_history(self):
        pane = (
            "Previous diagnostic output: Claude Code runtime mismatch\n"
            "More history about opus 4.6 and delivery retries\n\n"
            "\u203a Explain this codebase\n\n"
            "  gpt-5.4 xhigh \u00b7 88% left \u00b7 ~/ws/core/code/WF/agent-backbone"
        )
        result = infer_state_from_pane(pane)
        assert result.state == AgentState.IDLE

    def test_codex_placeholder_is_not_pending_input(self):
        pane = "\x1b[1m\u203a\x1b[0m \x1b[2mImprove documentation in @filename\x1b[0m"
        assert prompt_has_pending_input(pane) is False

    def test_codex_typed_input_is_pending_input(self):
        pane = "\u203a Review the delivery retry logic"
        assert prompt_has_pending_input(pane) is True

    def test_codex_queued_input_footer_is_ignored_for_prompt_detection(self):
        pane = (
            "\u203a Second live inbound delivery test.\n\n"
            "  tab to queue message                                        98% context left"
        )
        assert prompt_has_pending_input(pane) is True

    def test_stuck_backbone_envelope_not_pending_input(self):
        pane = "\u276f [via:backbone from:ike] Can you check the status?"
        assert prompt_has_pending_input(pane) is False

    def test_stuck_github_envelope_not_pending_input(self):
        pane = "\u276f [via:github issue:51] [task] agent-backbone: Add topic routing"
        assert prompt_has_pending_input(pane) is False

    def test_stuck_telegram_envelope_not_pending_input(self):
        pane = "\u276f [via:telegram from:elias] What's the status?"
        assert prompt_has_pending_input(pane) is False

    def test_stuck_heartbeat_envelope_not_pending_input(self):
        pane = "\u276f [via:heartbeat] periodic check"
        assert prompt_has_pending_input(pane) is False

    def test_real_user_input_still_detected(self):
        pane = "\u276f hello"
        assert prompt_has_pending_input(pane) is True

    def test_prefix_guard_suffix_matched_output_not_pending(self):
        claude = get_terminal_adapter(TerminalRuntime.CLAUDE)
        assert claude.prompt_has_pending_input("some output line $") is False

    def test_codex_queued_message_banner_is_ignored_for_prompt_detection(self):
        pane = (
            "\u2022 Messages to be submitted after next tool call "
            "(press esc to interrupt and send immediately)\n"
            "  \u21b3 [via:backbone from:bell] delivery check only.\n\n"
            "\u203a Summarize recent commits\n\n"
            "  gpt-5.4 xhigh \u00b7 59% left \u00b7 ~/ws/core/code/WF/agent-backbone"
        )
        assert prompt_has_pending_input(pane) is True

    def test_idle_standard_prompt_with_trailing_lines_is_not_idle(self):
        pane = "user@host $\nsome trailing output"
        result = infer_state_from_pane(pane)
        assert result.state == AgentState.OFFLINE

    def test_idle_prompt_overrides_stale_tool_call_history(self):
        pane = "Running tool call: Read\nprevious output\nuser@host $"
        result = infer_state_from_pane(pane)
        assert result.state == AgentState.IDLE


class TestRowToSnapshot:
    def test_converts_idle_row(self):
        row = {
            "session_name": "ike",
            "state": "idle",
            "current_issue": None,
            "ts": "1709500000.0",
            "started_at": None,
            "plan_file": None,
            "plan_title": None,
        }
        snap = _row_to_snapshot(row)
        assert snap.state == AgentState.IDLE
        assert snap.timestamp == 1709500000.0
        assert snap.source == "db"
        assert snap.current_issue is None

    def test_converts_legacy_processing_row(self):
        row = {
            "session_name": "feynman",
            "state": "processing_issue",
            "current_issue": 571,
            "ts": "1709500000.0",
            "started_at": "1709499000.0",
            "plan_file": "/tmp/plan.md",
            "plan_title": "DB migration",
        }
        snap = _row_to_snapshot(row)
        assert snap.state == AgentState.BUSY
        assert snap.current_issue == 571
        assert snap.started_at == 1709499000.0
        assert snap.plan_file == "/tmp/plan.md"
        assert snap.plan_title == "DB migration"

    def test_unknown_state_value_maps_to_offline(self):
        row = {"state": "sleeping", "ts": None}
        snap = _row_to_snapshot(row)
        assert snap.state == AgentState.OFFLINE

    def test_missing_ts_defaults_to_zero(self):
        row = {"state": "idle", "ts": None}
        snap = _row_to_snapshot(row)
        assert snap.timestamp == 0.0

    def test_parses_structured_context_json(self):
        row = {
            "state": "permission_waiting",
            "ts": "1709500000.0",
            "context": '{"tool":"Read","target":"/tmp/example","prompt":"Do you want to proceed?"}',
        }
        snap = _row_to_snapshot(row)
        assert snap.state == AgentState.PERMISSION_WAITING
        assert snap.context == {
            "tool": "Read",
            "target": "/tmp/example",
            "prompt": "Do you want to proceed?",
        }


class TestStateServiceDBFirst:
    @pytest.fixture
    async def db(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        db = BackboneDB(engine)
        await db.start()
        try:
            yield db
        finally:
            db._engine = None
            await engine.dispose()

    async def test_db_first_returns_idle_db_state(self, db):
        await db.set_agent_state("ike", "idle", current_issue=None, ts="1709500000.0")
        svc = StateService(db=db)
        snap = await svc.get_state("ike")
        assert snap.state == AgentState.IDLE
        assert snap.current_issue is None
        assert snap.source == "db"

    async def test_db_working_state_returns_reported_snapshot(self, db):
        await db.set_agent_state("ike", "busy", current_issue=42, ts="1709500000.0")
        svc = StateService(db=db)
        snap = await svc.get_state("ike")
        assert snap.state == AgentState.BUSY
        assert snap.current_issue == 42
        assert snap.source == "db"

    async def test_missing_db_row_uses_observed_offline_default(self, db):
        svc = StateService(db=db)
        with patch(
            f"{_IFACE}.observe_session",
            new_callable=AsyncMock,
            return_value=MagicMock(online=False, has_sub_agent_processes=False),
        ):
            snap = await svc.get_state("ike")
        assert snap.state == AgentState.OFFLINE
        assert snap.source == "observed"

    async def test_missing_db_row_uses_starting_default_when_session_is_online(self, db):
        svc = StateService(db=db)
        with patch(
            f"{_IFACE}.observe_session",
            new_callable=AsyncMock,
            return_value=MagicMock(online=True, has_sub_agent_processes=False),
        ):
            snap = await svc.get_state("ike")
        assert snap.state == AgentState.STARTING
        assert snap.source == "default"

    async def test_missing_db_service_uses_observed_offline_default(self):
        svc = StateService(db=None)
        with patch(
            f"{_IFACE}.observe_session",
            new_callable=AsyncMock,
            return_value=MagicMock(online=False, has_sub_agent_processes=False),
        ):
            snap = await svc.get_state("ike")
        assert snap.state == AgentState.OFFLINE
        assert snap.source == "observed"

    async def test_reported_offline_state_upgrades_to_starting_when_session_is_online(self, db):
        await db.set_agent_state("ike", "offline", ts="1709500000.0")
        svc = StateService(db=db)
        with patch(
            f"{_IFACE}.observe_session",
            new_callable=AsyncMock,
            return_value=MagicMock(online=True, has_sub_agent_processes=False),
        ):
            snap = await svc.get_state("ike")
        assert snap.state == AgentState.STARTING
        assert snap.timestamp == 1709500000.0

    async def test_db_error_surfaces(self):
        mock_db = MagicMock()
        mock_db.get_agent_state = AsyncMock(side_effect=RuntimeError("DB down"))
        svc = StateService(db=mock_db)
        with pytest.raises(RuntimeError, match="Failed to read agent state for ike"):
            await svc.get_reported_state("ike")


class TestShouldDeliver:
    def test_idle_always_delivers(self):
        assert should_deliver(AgentState.IDLE) is True

    @pytest.mark.parametrize(
        "state",
        [
            AgentState.STARTING,
            AgentState.BUSY,
            AgentState.PLAN_WAITING,
            AgentState.PERMISSION_WAITING,
            AgentState.SUB_AGENT_WAITING,
            AgentState.OFFLINE,
        ],
    )
    def test_non_idle_states_do_not_deliver(self, state):
        assert should_deliver(state) is False
        assert should_deliver(state, is_blocking=True) is False
        assert should_deliver(state, require_idle=True) is False


def _action_entry(session="ike", action="comment", issue=42, ts=None):
    """Helper to build a github-actions.jsonl entry."""
    return {
        "ts": ts if ts is not None else time.time(),
        "session": session,
        "action": action,
        "issue": issue,
    }


class TestFindOutgoingComment:
    def test_finds_recent_matching_comment(self, tmp_path):
        log_file = tmp_path / "github-actions.jsonl"
        log_file.write_text(json.dumps(_action_entry()) + "\n")
        result = find_outgoing_comment(42, action_log=str(log_file))
        assert result == "ike"

    def test_no_match_wrong_issue(self, tmp_path):
        log_file = tmp_path / "github-actions.jsonl"
        log_file.write_text(json.dumps(_action_entry(issue=42)) + "\n")
        result = find_outgoing_comment(99, action_log=str(log_file))
        assert result is None

    def test_stale_entry_ignored(self, tmp_path):
        log_file = tmp_path / "github-actions.jsonl"
        old_ts = time.time() - 60
        log_file.write_text(json.dumps(_action_entry(ts=old_ts)) + "\n")
        result = find_outgoing_comment(42, action_log=str(log_file), recency_seconds=30.0)
        assert result is None

    def test_missing_file_returns_none(self, tmp_path):
        result = find_outgoing_comment(42, action_log=str(tmp_path / "nonexistent.jsonl"))
        assert result is None

    def test_malformed_lines_skipped(self, tmp_path):
        log_file = tmp_path / "github-actions.jsonl"
        entry = _action_entry(session="leo", issue=10)
        log_file.write_text("not json\n" + json.dumps(entry) + "\n")
        result = find_outgoing_comment(10, action_log=str(log_file))
        assert result == "leo"

    def test_different_action_ignored(self, tmp_path):
        log_file = tmp_path / "github-actions.jsonl"
        entry = _action_entry(action="issue_update")
        log_file.write_text(json.dumps(entry) + "\n")
        result = find_outgoing_comment(42, action_log=str(log_file))
        assert result is None


class TestHasCommentedOnIssue:
    def test_finds_matching_comment(self, tmp_path):
        log_file = tmp_path / "github-actions.jsonl"
        entry = _action_entry(session="ike", issue=42, ts=time.time() - 3600)
        log_file.write_text(json.dumps(entry) + "\n")
        assert has_commented_on_issue(42, "ike", action_log=str(log_file)) is True

    def test_no_match_wrong_issue(self, tmp_path):
        log_file = tmp_path / "github-actions.jsonl"
        entry = _action_entry(session="ike", issue=42)
        log_file.write_text(json.dumps(entry) + "\n")
        assert has_commented_on_issue(99, "ike", action_log=str(log_file)) is False

    def test_missing_file_returns_false(self, tmp_path):
        assert (
            has_commented_on_issue(42, "ike", action_log=str(tmp_path / "nonexistent.jsonl"))
            is False
        )
