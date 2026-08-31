"""Tests for the shipped Claude Code state hook (stdlib-only script)."""

from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from agent_backbone.hooks import claude_hook as hook
from agent_backbone.services.agents._file_reader import read_state_file
from agent_backbone.services.agents.models import AgentState


def _payload(event: str, **extra) -> dict:
    return {"hook_event_name": event, "session_id": "abc", "cwd": "/tmp", **extra}


class TestDerive:
    @pytest.mark.parametrize(
        ("event", "expected"),
        [
            ("SessionStart", hook.STATE_IDLE),
            ("UserPromptSubmit", hook.STATE_BUSY),
            ("Stop", hook.STATE_IDLE),
            ("SessionEnd", hook.STATE_UNKNOWN),
        ],
    )
    def test_simple_events(self, event, expected):
        record, action = hook.derive(_payload(event), None)
        assert record["state"] == expected
        assert action is None

    def test_notification_permission(self):
        record, _ = hook.derive(
            _payload("Notification", message="Claude needs your permission to use Bash"), None
        )
        assert record["state"] == hook.STATE_PERMISSION_WAITING

    def test_notification_idle_prompt(self):
        record, _ = hook.derive(
            _payload("Notification", message="Claude is waiting for your input"), None
        )
        assert record["state"] == hook.STATE_IDLE

    def test_notification_other_is_ignored(self):
        assert hook.derive(_payload("Notification", message="something else"), None) == (None, None)

    def test_plan_mode_enter_and_exit(self):
        plan = "# Add caching\n\n1. do things"
        record, _ = hook.derive(
            _payload("PreToolUse", tool_name="ExitPlanMode", tool_input={"plan": plan}), None
        )
        assert record["state"] == hook.STATE_PLAN_WAITING
        assert record["plan_title"] == "Add caching"
        assert record["plan_text"] == plan

        record, _ = hook.derive(_payload("PostToolUse", tool_name="ExitPlanMode"), record)
        assert record["state"] == hook.STATE_BUSY

    def test_issue_number_captured_from_prompt(self):
        record, _ = hook.derive(
            _payload("UserPromptSubmit", prompt="New issue targeting you: acme/app#42 ..."), None
        )
        assert record["issue"] == 42
        # Preserved across later events
        later, _ = hook.derive(_payload("Stop"), record)
        assert later["issue"] == 42

    def test_started_at_is_stable(self):
        first, _ = hook.derive(_payload("SessionStart"), None)
        later, _ = hook.derive(_payload("Stop"), first)
        assert later["started_at"] == first["started_at"]

    @pytest.mark.parametrize(
        ("tool", "tool_input", "issue"),
        [
            ("Bash", {"command": "gh issue comment 17 --body 'done'"}, 17),
            ("Bash", {"command": "gh issue comment --repo a/b 9 -b x"}, 9),
            ("mcp__github__add_issue_comment", {"issue_number": 5}, 5),
            ("Bash", {"command": "ls"}, None),
            ("Edit", {"file_path": "x"}, None),
        ],
    )
    def test_comment_actions(self, tool, tool_input, issue):
        _, action = hook.derive(
            _payload("PostToolUse", tool_name=tool, tool_input=tool_input), None
        )
        if issue is None:
            assert action is None
        else:
            assert action["action"] == "comment" and action["issue"] == issue


class TestResolveAgent:
    def test_explicit_wins(self, monkeypatch):
        monkeypatch.setenv("BACKBONE_AGENT", "env-agent")
        assert hook.resolve_agent("flag-agent") == "flag-agent"

    def test_env(self, monkeypatch):
        monkeypatch.setenv("BACKBONE_AGENT", "env-agent")
        assert hook.resolve_agent(None) == "env-agent"

    def test_tmux_fallback(self, monkeypatch):
        monkeypatch.delenv("BACKBONE_AGENT", raising=False)
        monkeypatch.setenv("TMUX", "/tmp/tmux-1/default,1,0")
        fake = type("R", (), {"returncode": 0, "stdout": "reviewer\n"})()
        with patch.object(hook.subprocess, "run", return_value=fake):
            assert hook.resolve_agent(None) == "reviewer"

    def test_none_outside_tmux(self, monkeypatch):
        monkeypatch.delenv("BACKBONE_AGENT", raising=False)
        monkeypatch.delenv("TMUX", raising=False)
        assert hook.resolve_agent(None) is None


class TestMainWritesStateTheBackboneReads:
    def _run(self, tmp_path, payload: dict, agent: str = "reviewer") -> int:
        with patch.object(hook.sys, "stdin", io.StringIO(json.dumps(payload))):
            return hook.main(["--state-dir", str(tmp_path), "--agent", agent, "--tag", "x"])

    def test_roundtrip_with_file_reader(self, tmp_path):
        assert self._run(tmp_path, _payload("UserPromptSubmit", prompt="issue #3")) == 0
        snap = read_state_file(tmp_path, "reviewer")
        assert snap is not None
        assert snap.state == AgentState.BUSY
        assert snap.current_issue == 3
        assert snap.source == "push"

        assert self._run(tmp_path, _payload("Stop")) == 0
        assert read_state_file(tmp_path, "reviewer").state == AgentState.IDLE

    def test_plan_file_written(self, tmp_path):
        payload = _payload("PreToolUse", tool_name="ExitPlanMode", tool_input={"plan": "# P\nbody"})
        assert self._run(tmp_path, payload) == 0
        snap = read_state_file(tmp_path, "reviewer")
        assert snap.state == AgentState.PLAN_WAITING
        assert snap.plan_title == "P"
        assert open(snap.plan_file).read() == "# P\nbody"

    def test_action_log_appended(self, tmp_path):
        payload = _payload(
            "PostToolUse", tool_name="Bash", tool_input={"command": "gh issue comment 8 -b ok"}
        )
        assert self._run(tmp_path, payload) == 0
        line = json.loads((tmp_path / "actions.jsonl").read_text().strip())
        assert line["session"] == "reviewer" and line["issue"] == 8

    def test_bad_input_is_harmless(self, tmp_path):
        with patch.object(hook.sys, "stdin", io.StringIO("not json")):
            assert hook.main(["--state-dir", str(tmp_path), "--agent", "r"]) == 0
        assert not (tmp_path / "r.json").exists()

    def test_no_agent_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BACKBONE_AGENT", raising=False)
        monkeypatch.delenv("TMUX", raising=False)
        with patch.object(hook.sys, "stdin", io.StringIO(json.dumps(_payload("Stop")))):
            assert hook.main(["--state-dir", str(tmp_path)]) == 0
        assert list(tmp_path.iterdir()) == []
