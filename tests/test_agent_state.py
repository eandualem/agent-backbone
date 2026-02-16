"""Tests for src/agent_state.py."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, patch

from src.agent_state import (
    AgentState,
    find_outgoing_comment,
    get_agent_state,
    has_commented_on_issue,
    infer_state_from_pane,
    read_state_file,
    should_deliver,
)


class TestReadStateFile:
    def test_reads_valid_state(self, tmp_path):
        state_file = tmp_path / "ike.json"
        state_file.write_text(json.dumps({"state": "idle", "issue": None, "ts": time.time()}))
        result = read_state_file(tmp_path, "ike")
        assert result is not None
        assert result.state == AgentState.IDLE
        assert result.source == "push"

    def test_processing_with_issue(self, tmp_path):
        state_file = tmp_path / "ike.json"
        state_file.write_text(
            json.dumps({"state": "processing_issue", "issue": 42, "ts": time.time()})
        )
        result = read_state_file(tmp_path, "ike")
        assert result.state == AgentState.PROCESSING_ISSUE
        assert result.current_issue == 42

    def test_missing_file(self, tmp_path):
        result = read_state_file(tmp_path, "nobody")
        assert result is None

    def test_malformed_json(self, tmp_path):
        state_file = tmp_path / "bad.json"
        state_file.write_text("not json")
        result = read_state_file(tmp_path, "bad")
        assert result is None

    def test_unknown_state_value(self, tmp_path):
        state_file = tmp_path / "ike.json"
        state_file.write_text(json.dumps({"state": "sleeping", "ts": time.time()}))
        result = read_state_file(tmp_path, "ike")
        assert result.state == AgentState.UNKNOWN

    def test_plan_waiting_state(self, tmp_path):
        state_file = tmp_path / "ike.json"
        state_file.write_text(
            json.dumps(
                {
                    "state": "plan_waiting",
                    "issue": None,
                    "ts": time.time(),
                    "plan_file": "/tmp/plan.md",
                    "plan_title": "Add caching layer",
                }
            )
        )
        result = read_state_file(tmp_path, "ike")
        assert result is not None
        assert result.state == AgentState.PLAN_WAITING
        assert result.plan_file == "/tmp/plan.md"
        assert result.plan_title == "Add caching layer"


class TestPlanWaiting:
    def test_plan_waiting_enum_value(self):
        assert AgentState.PLAN_WAITING == "plan_waiting"
        assert AgentState("plan_waiting") == AgentState.PLAN_WAITING

    def test_should_deliver_plan_waiting_default(self):
        assert should_deliver(AgentState.PLAN_WAITING) is False

    def test_should_deliver_plan_waiting_blocking(self):
        assert should_deliver(AgentState.PLAN_WAITING, is_blocking=True) is False

    def test_should_deliver_plan_waiting_require_idle(self):
        assert should_deliver(AgentState.PLAN_WAITING, require_idle=True) is False


class TestInferStateFromPane:
    def test_empty_content(self):
        result = infer_state_from_pane("")
        assert result.state == AgentState.UNKNOWN

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
        content = "Starting task\nWorking on issue #42\nuser@host $"
        result = infer_state_from_pane(content)
        assert result.state == AgentState.PROCESSING_ISSUE
        assert result.current_issue == 42

    def test_unknown_content(self):
        result = infer_state_from_pane("random output with no indicators")
        assert result.state == AgentState.UNKNOWN

    def test_source_is_pull(self):
        result = infer_state_from_pane("user@host $")
        assert result.source == "pull"

    def test_idle_claude_code_prompt(self):
        """Claude Code uses ❯ (U+276F) as prompt character."""
        result = infer_state_from_pane("\u276f ")
        assert result.state == AgentState.IDLE

    def test_idle_claude_code_with_status_bar(self):
        """Claude Code renders a status bar below the prompt."""
        sep = "\u2500" * 11
        pane = f"\u276f \n{sep}\n  12 files +177 -1242"
        result = infer_state_from_pane(pane)
        assert result.state == AgentState.IDLE

    def test_idle_prompt_above_separator(self):
        """Prompt sandwiched between separator lines."""
        sep = "\u2500" * 7
        pane = f"{sep}\n\u276f \n{sep}\n  stats"
        result = infer_state_from_pane(pane)
        assert result.state == AgentState.IDLE

    def test_idle_standard_prompt_with_trailing_lines(self):
        """Non-prompt trailing content returns UNKNOWN."""
        pane = "user@host $\nsome trailing output"
        result = infer_state_from_pane(pane)
        assert result.state == AgentState.UNKNOWN


class TestGetAgentState:
    async def test_fresh_push_preferred(self, tmp_path):
        state_file = tmp_path / "ike.json"
        state_file.write_text(
            json.dumps({"state": "processing_issue", "issue": 42, "ts": time.time()})
        )
        with patch("src.agent_state.capture_pane", new_callable=AsyncMock):
            result = await get_agent_state(tmp_path, "ike")
        assert result.state == AgentState.PROCESSING_ISSUE
        assert result.source == "push"

    async def test_stale_push_triggers_pull(self, tmp_path):
        state_file = tmp_path / "ike.json"
        state_file.write_text(
            json.dumps({"state": "busy", "ts": time.time() - 600})  # 10 min old
        )
        with patch(
            "src.agent_state.capture_pane",
            new_callable=AsyncMock,
            return_value="user@host $",
        ):
            result = await get_agent_state(tmp_path, "ike", stale_threshold=300)
        assert result.state == AgentState.IDLE
        assert result.source == "pull"

    async def test_no_push_uses_pull(self, tmp_path):
        with patch(
            "src.agent_state.capture_pane",
            new_callable=AsyncMock,
            return_value="Thinking...\n",
        ):
            result = await get_agent_state(tmp_path, "nobody")
        assert result.state == AgentState.BUSY
        assert result.source == "pull"

    async def test_no_data_returns_unknown(self, tmp_path):
        with patch(
            "src.agent_state.capture_pane",
            new_callable=AsyncMock,
            return_value="",
        ):
            result = await get_agent_state(tmp_path, "nobody")
        assert result.state == AgentState.UNKNOWN
        assert result.source == "default"

    async def test_stale_idle_trusted(self, tmp_path):
        """Stale idle push state is trusted without pane capture (STATE-17)."""
        state_file = tmp_path / "ike.json"
        state_file.write_text(
            json.dumps({"state": "idle", "issue": None, "ts": time.time() - 600})
        )
        mock_capture = AsyncMock(return_value="random stuff")
        with patch("src.agent_state.capture_pane", mock_capture):
            result = await get_agent_state(tmp_path, "ike", stale_threshold=300)
        assert result.state == AgentState.IDLE
        assert result.source == "push"
        mock_capture.assert_not_called()

    async def test_stale_busy_triggers_pull(self, tmp_path):
        """Stale busy push state falls through to pane parsing."""
        state_file = tmp_path / "ike.json"
        state_file.write_text(
            json.dumps({"state": "busy", "issue": None, "ts": time.time() - 600})
        )
        with patch(
            "src.agent_state.capture_pane",
            new_callable=AsyncMock,
            return_value="user@host $",
        ):
            result = await get_agent_state(tmp_path, "ike", stale_threshold=300)
        assert result.state == AgentState.IDLE
        assert result.source == "pull"

    async def test_stale_processing_triggers_pull(self, tmp_path):
        """Stale processing_issue push state falls through to pane parsing."""
        state_file = tmp_path / "ike.json"
        state_file.write_text(
            json.dumps({"state": "processing_issue", "issue": 42, "ts": time.time() - 600})
        )
        with patch(
            "src.agent_state.capture_pane",
            new_callable=AsyncMock,
            return_value="user@host $",
        ):
            result = await get_agent_state(tmp_path, "ike", stale_threshold=300)
        assert result.state == AgentState.IDLE
        assert result.source == "pull"


class TestShouldDeliver:
    def test_idle_always_delivers(self):
        assert should_deliver(AgentState.IDLE) is True

    def test_starting_always_delivers(self):
        assert should_deliver(AgentState.STARTING) is True

    def test_unknown_always_delivers(self):
        assert should_deliver(AgentState.UNKNOWN) is True

    def test_processing_blocks_nonblocking(self):
        assert should_deliver(AgentState.PROCESSING_ISSUE, is_blocking=False) is False

    def test_processing_allows_blocking(self):
        assert should_deliver(AgentState.PROCESSING_ISSUE, is_blocking=True) is True

    def test_busy_never_delivers(self):
        assert should_deliver(AgentState.BUSY, is_blocking=False) is False

    def test_busy_blocks_even_blocking(self):
        assert should_deliver(AgentState.BUSY, is_blocking=True) is False


class TestRequireIdle:
    """Monitor mode: only idle agents should receive deliveries."""

    def test_idle_delivers(self):
        assert should_deliver(AgentState.IDLE, require_idle=True) is True

    def test_busy_skipped(self):
        assert should_deliver(AgentState.BUSY, require_idle=True) is False

    def test_starting_skipped(self):
        assert should_deliver(AgentState.STARTING, require_idle=True) is False

    def test_unknown_skipped(self):
        assert should_deliver(AgentState.UNKNOWN, require_idle=True) is False

    def test_processing_skipped(self):
        assert should_deliver(AgentState.PROCESSING_ISSUE, require_idle=True) is False

    def test_blocking_ignored_in_monitor_mode(self):
        """Even blocking issues don't override require_idle."""
        assert should_deliver(AgentState.BUSY, is_blocking=True, require_idle=True) is False


class TestCapacityRouting:
    def test_busy_short_nonblocking_defer(self):
        """Busy <30min + non-blocking → defer."""
        assert (
            should_deliver(
                AgentState.BUSY, is_blocking=False, busy_duration=600.0, busy_threshold=1800.0
            )
            is False
        )

    def test_busy_short_blocking_defer(self):
        """Busy <30min + blocking → defer (unchanged)."""
        assert (
            should_deliver(
                AgentState.BUSY, is_blocking=True, busy_duration=600.0, busy_threshold=1800.0
            )
            is False
        )

    def test_busy_long_blocking_deliver(self):
        """Busy >=30min + blocking → deliver (NEW capacity routing)."""
        assert (
            should_deliver(
                AgentState.BUSY, is_blocking=True, busy_duration=1800.0, busy_threshold=1800.0
            )
            is True
        )

    def test_busy_long_nonblocking_defer(self):
        """Busy >=30min + non-blocking → defer."""
        assert (
            should_deliver(
                AgentState.BUSY, is_blocking=False, busy_duration=3600.0, busy_threshold=1800.0
            )
            is False
        )

    def test_no_duration_fallback(self):
        """busy_duration=None → uses legacy behavior (defer)."""
        assert (
            should_deliver(
                AgentState.BUSY, is_blocking=True, busy_duration=None, busy_threshold=1800.0
            )
            is False
        )

    def test_started_at_parsed_from_state_file(self, tmp_path):
        """State file with started_at field is parsed correctly."""
        state_file = tmp_path / "ike.json"
        data = {"state": "busy", "issue": 42, "ts": time.time(), "started_at": 1700000000.0}
        state_file.write_text(json.dumps(data))
        result = read_state_file(tmp_path, "ike")
        assert result is not None
        assert result.started_at == 1700000000.0

    def test_started_at_absent_is_none(self, tmp_path):
        """State file without started_at → None."""
        state_file = tmp_path / "ike.json"
        state_file.write_text(json.dumps({"state": "busy", "ts": time.time()}))
        result = read_state_file(tmp_path, "ike")
        assert result is not None
        assert result.started_at is None


def _action_entry(session="ike", action="comment", issue=42, ts=None):
    """Helper to build a github-actions.jsonl entry.

    Matches real hook format: {ts, session, action, issue}.
    """
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
        """Entries older than recency_seconds should not match."""
        log_file = tmp_path / "github-actions.jsonl"
        old_ts = time.time() - 60  # 60 seconds ago
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
