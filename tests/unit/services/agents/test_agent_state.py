"""Tests for agent_backbone/services/agents (state tracking)."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from agent_backbone.services.agents import (
    AgentState,
    StateSnapshot,
    find_outgoing_comment,
    get_agent_state,
    has_commented_on_issue,
    infer_state_from_pane,
    read_state_file,
    should_deliver,
)
from agent_backbone.services.agents.interface import StateService, _row_to_snapshot
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.terminal import detect_runtime_from_pane, get_terminal_adapter

_INF = "agent_backbone.services.agents._inference"
_IFACE = "agent_backbone.services.agents.interface"


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
            "unknown",
        }

    def test_parse_is_total(self):
        assert AgentState.parse("busy") == AgentState.BUSY
        assert AgentState.parse("waiting_for_human") == AgentState.WAITING_FOR_HUMAN
        assert AgentState.parse("sleeping") == AgentState.UNKNOWN
        assert AgentState.parse(None) == AgentState.UNKNOWN

    def test_should_deliver_waiting(self):
        assert should_deliver(AgentState.WAITING_FOR_HUMAN) is False


def prompt_has_pending_input(pane: str) -> bool:
    """The adapter for whatever runtime the pane shows, asked about typed input."""
    return get_terminal_adapter(detect_runtime_from_pane(pane)).prompt_has_pending_input(pane)


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

    def test_codex_placeholder_is_not_pending_input(self):
        """Codex's dim placeholder suggestion should not count as typed input."""
        pane = "\x1b[1m\u203a\x1b[0m \x1b[2mImprove documentation in @filename\x1b[0m"
        assert prompt_has_pending_input(pane) is False

    def test_codex_typed_input_is_pending_input(self):
        """Actual typed Codex input should still block delivery."""
        pane = "\u203a Review the delivery retry logic"
        assert prompt_has_pending_input(pane) is True

    def test_codex_queued_input_footer_is_ignored_for_prompt_detection(self):
        """Codex's queue footer below the prompt should not hide pending input."""
        pane = (
            "\u203a Second live inbound delivery test.\n\n"
            "  tab to queue message                                        98% context left"
        )
        assert prompt_has_pending_input(pane) is True

    def test_stuck_backbone_envelope_not_pending_input(self):
        """A stuck backbone delivery in the prompt buffer is not user input (#766)."""
        pane = "\u276f [via:backbone from:ike] Can you check the status?"
        assert prompt_has_pending_input(pane) is False

    def test_stuck_github_envelope_not_pending_input(self):
        """Stuck github notification envelope is not user input."""
        pane = "\u276f [via:github issue:51] [task] agent-backbone: Add topic routing"
        assert prompt_has_pending_input(pane) is False

    def test_stuck_telegram_envelope_not_pending_input(self):
        """Stuck telegram envelope is not user input."""
        pane = "\u276f [via:telegram from:elias] What's the status?"
        assert prompt_has_pending_input(pane) is False

    def test_stuck_heartbeat_envelope_not_pending_input(self):
        """Stuck heartbeat envelope is not user input."""
        pane = "\u276f [via:heartbeat] periodic check"
        assert prompt_has_pending_input(pane) is False

    def test_real_user_input_still_detected(self):
        """Regression guard: actual user text after prompt is still pending input."""
        pane = "\u276f hello"
        assert prompt_has_pending_input(pane) is True

    def test_prefix_guard_suffix_matched_output_not_pending(self):
        """A suffix-matched output line (no prompt prefix) is not pending input."""
        from agent_backbone.services.terminal._adapters import TerminalRuntime, get_terminal_adapter

        # Claude adapter has prefix ❯ and suffix $. A line ending with $
        # but not starting with ❯ matched via suffix only — prefix guard
        # should reject it as not real user input.
        claude = get_terminal_adapter(TerminalRuntime.CLAUDE)
        assert claude.prompt_has_pending_input("some output line $") is False

    def test_codex_queued_message_banner_is_ignored_for_prompt_detection(self):
        """Queued-message instructional chrome should not hide the live prompt."""
        pane = (
            "\u2022 Messages to be submitted after next tool call "
            "(press esc to interrupt and send immediately)\n"
            "  \u21b3 [via:backbone from:bell] delivery check only.\n\n"
            "\u203a Summarize recent commits\n\n"
            "  gpt-5.4 xhigh \u00b7 59% left \u00b7 ~/ws/core/code/WF/agent-backbone"
        )
        assert prompt_has_pending_input(pane) is True

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

    def test_converts_processing_row(self):
        row = {
            "session_name": "feynman",
            "state": "busy",
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

    def test_unknown_state_value_maps_to_unknown(self):
        row = {"state": "sleeping", "ts": None}
        snap = _row_to_snapshot(row)
        assert snap.state == AgentState.UNKNOWN

    def test_missing_ts_defaults_to_zero(self):
        row = {"state": "idle", "ts": None}
        snap = _row_to_snapshot(row)
        assert snap.timestamp == 0.0


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

    async def test_db_first_returns_recent_idle_db_state(self, db, tmp_path):
        """Recent stable idle snapshots are served from DB without live reconciliation."""
        await db.set_agent_state("ike", "idle", current_issue=None, ts=str(time.time()))
        svc = StateService(state_dir=str(tmp_path), db=db)
        snap = await svc.get_state("ike")
        assert snap.state == AgentState.IDLE
        assert snap.current_issue is None
        assert snap.source == "db"

    async def test_old_idle_db_state_is_reverified_live(self, db, tmp_path):
        """A stored snapshot older than the trust window is re-verified live."""
        await db.set_agent_state("ike", "idle", current_issue=None, ts=str(time.time() - 120))
        svc = StateService(state_dir=str(tmp_path), db=db, snapshot_trust=20)
        live_snapshot = StateSnapshot(state=AgentState.BUSY, source="pull", timestamp=time.time())
        with patch(
            f"{_IFACE}._get_agent_state",
            new_callable=AsyncMock,
            return_value=live_snapshot,
        ):
            snap = await svc.get_state("ike")
        assert snap.state == AgentState.BUSY
        row = await db.get_agent_state("ike")
        assert row["state"] == "busy"

    async def test_fresh_hook_state_overrides_recent_db_snapshot(self, db, tmp_path):
        """A hook state file newer than the stored snapshot wins over the DB shortcut."""
        await db.set_agent_state("ike", "idle", current_issue=None, ts=str(time.time() - 5))
        (tmp_path / "ike.json").write_text(json.dumps({"state": "busy", "ts": time.time()}))
        svc = StateService(state_dir=str(tmp_path), db=db, snapshot_trust=20)
        snap = await svc.get_state("ike")
        assert snap.state == AgentState.BUSY
        assert snap.source == "push"

    async def test_db_working_state_uses_live_reconciliation(self, db, tmp_path):
        """Cached working states are refreshed from the live reconciler."""
        await db.set_agent_state("ike", "busy", current_issue=42, ts="1709500000.0")
        svc = StateService(state_dir=str(tmp_path), db=db)
        live_snapshot = StateSnapshot(
            state=AgentState.IDLE,
            source="pull",
            timestamp=1709500100.0,
        )
        with patch(
            f"{_IFACE}._get_agent_state",
            new_callable=AsyncMock,
            return_value=live_snapshot,
        ):
            snap = await svc.get_state("ike")
        assert snap.state == AgentState.IDLE
        assert snap.source == "pull"

        row = await db.get_agent_state("ike")
        assert row is not None
        assert row["state"] == "idle"
        assert row["current_issue"] is None
        assert row["ts"] == "1709500100.0"

    async def test_fallback_to_file_when_no_db_row(self, db, tmp_path):
        """When DB has no row, falls back to file+tmux."""
        state_file = tmp_path / "ike.json"
        state_file.write_text(json.dumps({"state": "idle", "issue": None, "ts": time.time()}))
        svc = StateService(state_dir=str(tmp_path), db=db)
        with patch(f"{_INF}.capture_pane", new_callable=AsyncMock):
            snap = await svc.get_state("ike")
        assert snap.state == AgentState.IDLE
        assert snap.source == "push"

    async def test_fallback_when_no_db(self, tmp_path):
        """When StateService has no db, uses file+tmux."""
        state_file = tmp_path / "ike.json"
        state_file.write_text(json.dumps({"state": "busy", "ts": time.time()}))
        svc = StateService(state_dir=str(tmp_path), db=None)
        with patch(f"{_INF}.capture_pane", new_callable=AsyncMock, return_value="random output"):
            snap = await svc.get_state("ike")
        assert snap.state == AgentState.BUSY
        assert snap.source == "push"

    async def test_db_error_falls_back_to_file(self, tmp_path):
        """When DB raises an error, falls back to file."""
        state_file = tmp_path / "ike.json"
        state_file.write_text(json.dumps({"state": "idle", "ts": time.time()}))
        mock_db = MagicMock()
        mock_db.get_agent_state = AsyncMock(side_effect=RuntimeError("DB down"))
        svc = StateService(state_dir=str(tmp_path), db=mock_db)
        with patch(f"{_INF}.capture_pane", new_callable=AsyncMock):
            snap = await svc.get_state("ike")
        assert snap.state == AgentState.IDLE
        assert snap.source == "push"


class TestShouldDeliver:
    def test_idle_always_delivers(self):
        assert should_deliver(AgentState.IDLE) is True

    def test_starting_deferred(self):
        assert should_deliver(AgentState.STARTING) is False

    def test_unknown_deferred(self):
        assert should_deliver(AgentState.UNKNOWN) is False

    def test_busy_never_delivers(self):
        assert should_deliver(AgentState.BUSY) is False


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


class TestStartingMarker:
    async def test_fresh_starting_marker_is_trusted(self, tmp_path):
        import json
        import time

        from agent_backbone.services.agents import get_agent_state

        (tmp_path / "ike.json").write_text(json.dumps({"state": "starting", "ts": time.time()}))
        snap = await get_agent_state(tmp_path, "ike", 300, pane_content="$ ")
        assert snap.state == AgentState.STARTING and snap.source == "push"

    async def test_old_starting_marker_yields_to_the_terminal(self, tmp_path):
        import json
        import time

        from agent_backbone.services.agents import get_agent_state

        (tmp_path / "ike.json").write_text(
            json.dumps({"state": "starting", "ts": time.time() - 200})
        )
        snap = await get_agent_state(tmp_path, "ike", 300, pane_content="$ ")
        assert snap.state == AgentState.IDLE and snap.source == "pull"

    async def test_old_starting_marker_is_not_trusted_when_the_pane_says_nothing(self, tmp_path):
        import json
        import time

        from agent_backbone.services.agents import get_agent_state

        (tmp_path / "ike.json").write_text(
            json.dumps({"state": "starting", "ts": time.time() - 200})
        )
        snap = await get_agent_state(tmp_path, "ike", 300, pane_content="")
        assert snap.state == AgentState.UNKNOWN
