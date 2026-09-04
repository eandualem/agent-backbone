"""Tests for agent_backbone/services/agents (state tracking)."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from agent_backbone.config import AgentsConfig
from agent_backbone.services.agents import (
    AgentState,
    agent_state,
    find_outgoing_comment,
    find_outgoing_pull_request,
    get_agent_state,
    has_commented_on_issue,
    infer_state_from_pane,
    read_state_file,
    rotate_action_log,
)

_INF = "agent_backbone.services.agents._inference"


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
        state_file.write_text(json.dumps({"state": "busy", "issue": 42, "ts": time.time()}))
        result = read_state_file(tmp_path, "ike")
        assert result.state == AgentState.BUSY
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
                    "state": "waiting_for_human",
                    "reason": "plan",
                    "issue": None,
                    "ts": time.time(),
                    "plan_file": "/tmp/plan.md",
                    "plan_title": "Add caching layer",
                }
            )
        )
        result = read_state_file(tmp_path, "ike")
        assert result is not None
        assert result.state == AgentState.WAITING_FOR_HUMAN
        assert result.reason == "plan"
        assert result.is_plan_waiting
        assert result.plan_file == "/tmp/plan.md"
        assert result.plan_title == "Add caching layer"

    def test_permission_waiting_state(self, tmp_path):
        state_file = tmp_path / "ike.json"
        state_file.write_text(
            json.dumps(
                {
                    "state": "waiting_for_human",
                    "reason": "permission",
                    "issue": 42,
                    "ts": time.time(),
                }
            )
        )
        result = read_state_file(tmp_path, "ike")
        assert result is not None
        assert result.state == AgentState.WAITING_FOR_HUMAN
        assert result.reason == "permission"
        assert result.current_issue == 42

    def test_generic_waiting_state_with_reason_and_repo(self, tmp_path):
        state_file = tmp_path / "ike.json"
        state_file.write_text(
            json.dumps(
                {
                    "state": "waiting_for_human",
                    "reason": "question",
                    "issue": 7,
                    "repo": "acme/app",
                    "ts": time.time(),
                }
            )
        )
        result = read_state_file(tmp_path, "ike")
        assert result.state == AgentState.WAITING_FOR_HUMAN
        assert result.reason == "question"
        assert result.current_repo == "acme/app"
        assert result.evidence


class TestWaitingForHuman:
    def test_enum_values_are_generic(self):
        assert {s.value for s in AgentState} == {
            "starting",
            "idle",
            "busy",
            "waiting_for_human",
            "blocked",
            "unknown",
        }

    def test_parse_is_total(self):
        assert AgentState.parse("busy") == AgentState.BUSY
        assert AgentState.parse("waiting_for_human") == AgentState.WAITING_FOR_HUMAN
        assert AgentState.parse("sleeping") == AgentState.UNKNOWN
        assert AgentState.parse(None) == AgentState.UNKNOWN


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
        assert result.state == AgentState.IDLE
        assert result.current_issue is None

    def test_unknown_content(self):
        result = infer_state_from_pane("random output with no indicators")
        assert result.state == AgentState.UNKNOWN
        assert result.evidence

    def test_claude_permission_prompt_is_waiting_for_human(self):
        pane = (
            "Bash command\n  rm -rf build\n\n"
            "Do you want to proceed?\n❯ 1. Yes\n  2. Yes, and don't ask again\n  3. No"
        )
        result = infer_state_from_pane(pane, runtime_hint="claude")
        assert result.state == AgentState.WAITING_FOR_HUMAN
        assert result.reason == "permission"

    def test_claude_busy_marker_wins_over_prompt(self):
        pane = "❯ \n────\n  Thinking… (esc to interrupt)"
        result = infer_state_from_pane(pane, runtime_hint="claude")
        assert result.state == AgentState.BUSY

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

    def test_idle_claude_code_prompt_with_ansi_formatting(self):
        """Rich-formatted prompt output should still resolve to IDLE."""
        pane = "\x1b[39m\u276f\xa0\x1b[7m \x1b[0m"
        result = infer_state_from_pane(pane)
        assert result.state == AgentState.IDLE

    def test_idle_codex_prompt_marker(self):
        """Codex uses › (U+203A) as its prompt marker."""
        result = infer_state_from_pane("\u203a ")
        assert result.state == AgentState.IDLE

    def test_idle_codex_prompt_with_buffered_input(self):
        """A visible Codex input line still means the session is at a prompt."""
        pane = "\u203a Improve documentation in @filename"
        result = infer_state_from_pane(pane)
        assert result.state == AgentState.IDLE

    def test_idle_codex_prompt_with_status_line_below(self):
        """Codex renders a status line below the prompt that should be ignored."""
        pane = (
            "\u203a Improve documentation in @filename\n\n"
            "  gpt-5.4 xhigh \u00b7 91% left \u00b7 ~/ws/core/code/WF/agent-orchestration-dashboard"
        )
        result = infer_state_from_pane(pane)
        assert result.state == AgentState.IDLE

    def test_idle_codex_prompt_ignores_stale_claude_history(self):
        """Historical runtime text above the prompt must not force a wrong adapter."""
        pane = (
            "Previous diagnostic output: Claude Code runtime mismatch\n"
            "More history about opus 4.6 and delivery retries\n\n"
            "\u203a Explain this codebase\n\n"
            "  gpt-5.4 xhigh \u00b7 88% left \u00b7 ~/ws/core/code/WF/agent-backbone"
        )
        result = infer_state_from_pane(pane)
        assert result.state == AgentState.IDLE

    def test_idle_standard_prompt_with_trailing_lines(self):
        """Non-prompt trailing content returns UNKNOWN."""
        pane = "user@host $\nsome trailing output"
        result = infer_state_from_pane(pane)
        assert result.state == AgentState.UNKNOWN

    def test_idle_prompt_overrides_stale_tool_call_history(self):
        """Old activity text above a live prompt should not force BUSY."""
        pane = "Running tool call: Read\nprevious output\nuser@host $"
        result = infer_state_from_pane(pane)
        assert result.state == AgentState.IDLE


class TestGetAgentState:
    async def test_configured_runtime_guides_terminal_fallback(self, config):
        spec = replace(config.agents.get("ike"), runtime="gemini")
        configured = replace(config, agents=AgentsConfig({**config.agents.specs, "ike": spec}))
        pane = "Generating response\nesc to cancel"

        with patch(f"{_INF}.capture_pane", new_callable=AsyncMock, return_value=pane):
            result = await agent_state(configured, "ike")

        assert result.state == AgentState.BUSY
        assert "(gemini)" in result.evidence[-1]

    async def test_fresh_push_preferred(self, tmp_path):
        state_file = tmp_path / "ike.json"
        state_file.write_text(json.dumps({"state": "busy", "issue": 42, "ts": time.time()}))
        with patch(f"{_INF}.capture_pane", new_callable=AsyncMock, return_value="random output"):
            result = await get_agent_state(tmp_path, "ike")
        assert result.state == AgentState.BUSY
        assert result.source == "push"
        assert any("fresh" in line for line in result.evidence)

    async def test_fresh_busy_push_is_trusted_even_when_pane_shows_prompt(self, tmp_path):
        """Hooks win: modern CLIs keep the prompt visible while working."""
        state_file = tmp_path / "ike.json"
        state_file.write_text(json.dumps({"state": "busy", "ts": time.time()}))
        with patch(
            f"{_INF}.capture_pane", new_callable=AsyncMock, return_value="\u276f "
        ) as capture:
            result = await get_agent_state(tmp_path, "ike")
        assert result.state == AgentState.BUSY
        assert result.source == "push"
        capture.assert_not_called()

    async def test_fresh_idle_push_is_vetoed_by_a_dialog_on_screen(self, tmp_path):
        """Claude Code's resume picker shows after SessionStart already said idle."""
        state_file = tmp_path / "ike.json"
        state_file.write_text(json.dumps({"state": "idle", "ts": time.time()}))
        picker = (
            "  We recommend resuming from a summary.\n"
            "  ❯ 1. Resume from summary (recommended)\n"
            "    2. Resume full session as-is\n"
            "  Enter to confirm · Esc to cancel\n"
        )
        with patch(f"{_INF}.capture_pane", new_callable=AsyncMock, return_value=picker):
            result = await get_agent_state(tmp_path, "ike", runtime_hint="claude")
        assert result.state == AgentState.WAITING_FOR_HUMAN
        assert result.reason == "question"
        assert result.source == "pull"
        assert any("fresh" in line for line in result.evidence)
        assert any("beats the hook" in line for line in result.evidence)
        assert result.timestamp > 0  # the observation time is kept for the database

    async def test_fresh_idle_push_stands_when_the_terminal_shows_a_prompt(self, tmp_path):
        state_file = tmp_path / "ike.json"
        state_file.write_text(json.dumps({"state": "idle", "ts": time.time()}))
        with patch(
            f"{_INF}.capture_pane", new_callable=AsyncMock, return_value="❯ \n  ? for shortcuts\n"
        ):
            result = await get_agent_state(tmp_path, "ike", runtime_hint="claude")
        assert result.state == AgentState.IDLE
        assert result.source == "push"

    async def test_fresh_processing_push_is_trusted(self, tmp_path):
        state_file = tmp_path / "ike.json"
        state_file.write_text(json.dumps({"state": "busy", "issue": 42, "ts": time.time()}))
        with patch(f"{_INF}.capture_pane", new_callable=AsyncMock, return_value="user@host $"):
            result = await get_agent_state(tmp_path, "ike")
        assert result.state == AgentState.BUSY
        assert result.current_issue == 42

    async def test_fresh_busy_push_survives_unknown_pane(self, tmp_path):
        state_file = tmp_path / "ike.json"
        state_file.write_text(json.dumps({"state": "busy", "ts": time.time()}))
        with patch(
            f"{_INF}.capture_pane",
            new_callable=AsyncMock,
            return_value="random output",
        ):
            result = await get_agent_state(tmp_path, "ike")
        assert result.state == AgentState.BUSY
        assert result.source == "push"

    async def test_stale_push_triggers_pull(self, tmp_path):
        """Stale push for states NOT in the trusted set triggers pane capture."""
        state_file = tmp_path / "ike.json"
        state_file.write_text(
            json.dumps({"state": "waiting_for_human", "reason": "plan", "ts": time.time() - 600})
        )
        with patch(
            f"{_INF}.capture_pane",
            new_callable=AsyncMock,
            return_value="user@host $",
        ):
            result = await get_agent_state(tmp_path, "ike", stale_threshold=300)
        assert result.state == AgentState.IDLE
        assert result.source == "pull"

    async def test_no_push_uses_pull(self, tmp_path):
        with patch(
            f"{_INF}.capture_pane",
            new_callable=AsyncMock,
            return_value="Thinking...\n",
        ):
            result = await get_agent_state(tmp_path, "nobody")
        assert result.state == AgentState.BUSY
        assert result.source == "pull"

    async def test_no_data_returns_unknown(self, tmp_path):
        with patch(
            f"{_INF}.capture_pane",
            new_callable=AsyncMock,
            return_value="",
        ):
            result = await get_agent_state(tmp_path, "nobody")
        assert result.state == AgentState.UNKNOWN
        assert result.source == "default"

    async def test_stale_idle_verified_via_tmux(self, tmp_path):
        """Stale known push state is reused when pane inference cannot classify."""
        state_file = tmp_path / "ike.json"
        state_file.write_text(json.dumps({"state": "idle", "issue": None, "ts": time.time() - 600}))
        mock_capture = AsyncMock(return_value="random stuff")
        with patch(f"{_INF}.capture_pane", mock_capture):
            result = await get_agent_state(tmp_path, "ike", stale_threshold=300)
        assert result.state == AgentState.IDLE
        assert result.source == "push"
        mock_capture.assert_awaited_once()

    async def test_stale_busy_verified_via_tmux(self, tmp_path):
        """Stale busy push state is re-checked from tmux before fallback."""
        state_file = tmp_path / "ike.json"
        state_file.write_text(json.dumps({"state": "busy", "issue": None, "ts": time.time() - 600}))
        mock_capture = AsyncMock(return_value="user@host $")
        with patch(f"{_INF}.capture_pane", mock_capture):
            result = await get_agent_state(tmp_path, "ike", stale_threshold=300)
        assert result.state == AgentState.IDLE
        assert result.source == "pull"
        mock_capture.assert_awaited_once()

    async def test_stale_processing_verified_via_tmux(self, tmp_path):
        """Stale processing_issue push state is re-checked from tmux before fallback."""
        state_file = tmp_path / "ike.json"
        state_file.write_text(json.dumps({"state": "busy", "issue": 42, "ts": time.time() - 600}))
        mock_capture = AsyncMock(return_value="user@host $")
        with patch(f"{_INF}.capture_pane", mock_capture):
            result = await get_agent_state(tmp_path, "ike", stale_threshold=300)
        assert result.state == AgentState.IDLE
        assert result.source == "pull"
        mock_capture.assert_awaited_once()

    async def test_stale_plan_waiting_without_plan_file_does_not_resurrect(self, tmp_path):
        """A missing plan file must not keep a dead plan_waiting snapshot alive."""
        state_file = tmp_path / "feynman.json"
        state_file.write_text(
            json.dumps(
                {
                    "state": "waiting_for_human",
                    "reason": "plan",
                    "ts": time.time() - 600,
                    "plan_file": str(tmp_path / "missing-plan.md"),
                    "plan_title": "Add caching",
                }
            )
        )
        with patch(f"{_INF}.capture_pane", new_callable=AsyncMock, return_value="random output"):
            result = await get_agent_state(tmp_path, "feynman", stale_threshold=300)
        assert result.state == AgentState.UNKNOWN
        assert result.source == "pull"

    async def test_stale_plan_waiting_with_existing_plan_file_falls_back(self, tmp_path):
        """An unresolved plan can still be trusted when the backing file exists."""
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("# Plan\n")
        state_file = tmp_path / "feynman.json"
        state_file.write_text(
            json.dumps(
                {
                    "state": "waiting_for_human",
                    "reason": "plan",
                    "ts": time.time() - 600,
                    "plan_file": str(plan_file),
                    "plan_title": "Add caching",
                }
            )
        )
        with patch(f"{_INF}.capture_pane", new_callable=AsyncMock, return_value="random output"):
            result = await get_agent_state(tmp_path, "feynman", stale_threshold=300)
        assert result.state == AgentState.WAITING_FOR_HUMAN
        assert result.reason == "plan"
        assert result.source == "push"

    async def test_stale_permission_waiting_does_not_resurrect(self, tmp_path):
        """Permission prompts are transient and must never be revived from stale push data."""
        state_file = tmp_path / "feynman.json"
        state_file.write_text(
            json.dumps(
                {
                    "state": "waiting_for_human",
                    "reason": "permission",
                    "issue": 42,
                    "ts": time.time() - 600,
                }
            )
        )
        with patch(f"{_INF}.capture_pane", new_callable=AsyncMock, return_value="random output"):
            result = await get_agent_state(tmp_path, "feynman", stale_threshold=300)
        assert result.state == AgentState.UNKNOWN
        assert result.source == "pull"


class TestStartedAt:
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


class TestRotateActionLog:
    def _log(self, tmp_path, lines: int):
        path = tmp_path / "actions.jsonl"
        path.write_text(
            "".join(
                json.dumps({"ts": float(i), "session": "ike", "action": "comment", "issue": i})
                + "\n"
                for i in range(lines)
            )
        )
        return path

    def test_keeps_the_newest_entries(self, tmp_path):
        path = self._log(tmp_path, 50)
        assert rotate_action_log(path, keep_lines=10) == 40
        kept = [json.loads(ln) for ln in path.read_text().splitlines()]
        assert [e["issue"] for e in kept] == list(range(40, 50))

    def test_short_log_untouched(self, tmp_path):
        path = self._log(tmp_path, 5)
        assert rotate_action_log(path, keep_lines=10) == 0
        assert len(path.read_text().splitlines()) == 5

    def test_missing_log_is_noop(self, tmp_path):
        assert rotate_action_log(tmp_path / "nope.jsonl") == 0

    def test_lookup_reads_only_the_tail(self, tmp_path):
        """A large log is not read whole: an old entry beyond the tail is invisible."""
        path = self._log(tmp_path, 20000)
        assert has_commented_on_issue(19999, "ike", path) is True
        assert has_commented_on_issue(0, "ike", path) is False


class TestStartingMarker:
    async def test_fresh_starting_marker_is_trusted(self, tmp_path):
        import time

        from agent_backbone.services.agents import get_agent_state, write_starting_marker

        write_starting_marker(tmp_path, "ike", time.time())
        snap = await get_agent_state(tmp_path, "ike", 300, pane_content="$ ")
        assert snap.state == AgentState.STARTING and snap.source == "push"

    async def test_old_starting_marker_yields_to_the_terminal(self, tmp_path):
        import time

        from agent_backbone.services.agents import get_agent_state, write_starting_marker

        write_starting_marker(tmp_path, "ike", time.time() - 200)
        snap = await get_agent_state(tmp_path, "ike", 300, pane_content="$ ")
        assert snap.state == AgentState.IDLE and snap.source == "pull"

    async def test_old_starting_marker_is_not_trusted_when_the_pane_says_nothing(self, tmp_path):
        import time

        from agent_backbone.services.agents import get_agent_state, write_starting_marker

        write_starting_marker(tmp_path, "ike", time.time() - 200)
        snap = await get_agent_state(tmp_path, "ike", 300, pane_content="")
        assert snap.state == AgentState.UNKNOWN

    def test_hook_state_newer_than_the_marker_wins_and_retires_it(self, tmp_path):
        import json
        import time

        from agent_backbone.services.agents import read_state_file, write_starting_marker

        launched = time.time() - 5
        write_starting_marker(tmp_path, "ike", launched)
        (tmp_path / "ike.json").write_text(json.dumps({"state": "idle", "ts": launched + 2}))
        snap = read_state_file(tmp_path, "ike")
        assert snap.state == AgentState.IDLE
        assert not (tmp_path / "ike.starting").exists()

    def test_marker_outranks_older_hook_state(self, tmp_path):
        import json
        import time

        from agent_backbone.services.agents import read_state_file, write_starting_marker

        # A leftover idle file from the previous run of this agent.
        (tmp_path / "ike.json").write_text(json.dumps({"state": "idle", "ts": time.time() - 900}))
        write_starting_marker(tmp_path, "ike", time.time())
        assert read_state_file(tmp_path, "ike").state == AgentState.STARTING


def test_rotation_tolerates_invalid_utf8(tmp_path):
    path = tmp_path / "actions.jsonl"
    lines = b"".join(b'{"issue": %d}\n' % i for i in range(20))
    path.write_bytes(lines + b"\xff\xfe not json\n")
    assert rotate_action_log(path, keep_lines=5) == 16
    assert len(path.read_text(errors="replace").splitlines()) == 5


class TestFindOutgoingPullRequest:
    def _log(self, tmp_path, *entries: dict):
        path = tmp_path / "actions.jsonl"
        path.write_text("".join(json.dumps(e) + "\n" for e in entries))
        return path

    def test_matches_head_repository_and_branch(self, tmp_path):
        log = self._log(
            tmp_path,
            {
                "ts": time.time(),
                "session": "app",
                "action": "pull_request",
                "repo": "acme/app",
                "head_repo": "forker/app",
                "branch": "feat/x",
            },
        )
        assert find_outgoing_pull_request("forker/app", "feat/x", action_log=log) == "app"
        # the same branch name from another fork is not it
        assert find_outgoing_pull_request("other/app", "feat/x", action_log=log) is None
        assert find_outgoing_pull_request("forker/app", "feat/other", action_log=log) is None

    def test_an_event_without_a_head_repository_falls_back_to_the_base(self, tmp_path):
        """A fork deleted before the event arrives: GitHub names no head repo,
        so the router passes the base one — the entry's `repo` still matches."""
        log = self._log(
            tmp_path,
            {
                "ts": time.time(),
                "session": "app",
                "action": "pull_request",
                "repo": "acme/app",
                "head_repo": "forker/app",
                "branch": "feat/x",
            },
        )
        assert find_outgoing_pull_request("acme/app", "feat/x", action_log=log) == "app"

    def test_an_older_entry_without_head_repo_matches_on_repo(self, tmp_path):
        log = self._log(
            tmp_path,
            {
                "ts": time.time(),
                "session": "app",
                "action": "pull_request",
                "repo": "acme/app",
                "branch": "feat/x",
            },
        )
        assert find_outgoing_pull_request("acme/app", "feat/x", action_log=log) == "app"

    def test_old_entries_and_missing_fields_do_not_match(self, tmp_path):
        log = self._log(
            tmp_path,
            {
                "ts": time.time() - 3600,
                "session": "app",
                "action": "pull_request",
                "repo": "acme/app",
                "branch": "feat/x",
            },
            {"ts": time.time(), "session": "app", "action": "pull_request", "repo": "acme/app"},
        )
        assert find_outgoing_pull_request("acme/app", "feat/x", action_log=log) is None
        assert find_outgoing_pull_request("", "feat/x", action_log=log) is None
