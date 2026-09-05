"""The shipped Codex hook (stdlib-only script): Codex events → shared states."""

from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from agent_backbone.hooks import backbone_state as bb
from agent_backbone.hooks import codex_hook as hook
from agent_backbone.services.agents._file_reader import read_state_file
from agent_backbone.services.agents.models import AgentState


def _payload(event: str, **extra) -> dict:
    return {"hook_event_name": event, "session_id": "01a0-codex", "cwd": "/tmp", **extra}


@pytest.fixture(autouse=True)
def _no_backbone_env(monkeypatch):
    monkeypatch.delenv("BACKBONE_STATE_DIR", raising=False)


class TestDerive:
    @pytest.mark.parametrize(
        ("event", "expected"),
        [
            ("SessionStart", bb.STATE_IDLE),
            ("UserPromptSubmit", bb.STATE_BUSY),
            ("PreToolUse", bb.STATE_BUSY),
            ("Stop", bb.STATE_IDLE),
            ("Interrupt", bb.STATE_IDLE),
            ("SessionEnd", bb.STATE_UNKNOWN),
        ],
    )
    def test_states(self, event, expected):
        record, action = hook.derive(_payload(event), None)
        assert record["state"] == expected and action is None
        assert record["event"] == event and record["session_id"] == "01a0-codex"

    def test_permission_request_is_waiting(self):
        record, _ = hook.derive(_payload("PermissionRequest", tool_name="Bash"), None)
        assert record["state"] == bb.STATE_WAITING and record["reason"] == bb.REASON_PERMISSION

    def test_stop_keeps_the_last_message_clipped(self):
        record, _ = hook.derive(_payload("Stop", last_assistant_message="x" * 600), None)
        assert record["last_message"].startswith("x" * 500) and len(record["last_message"]) == 501
        later, _ = hook.derive(_payload("PreToolUse"), record)
        assert later["last_message"] == record["last_message"]  # carried until the next Stop

    def test_issue_from_prompt_survives_later_events(self):
        record, _ = hook.derive(_payload("UserPromptSubmit", prompt="work on acme/app#7"), None)
        assert (record["issue"], record["repo"]) == (7, "acme/app")
        later, _ = hook.derive(_payload("Stop"), record)
        assert (later["issue"], later["repo"]) == (7, "acme/app")

    @pytest.mark.parametrize(
        ("tool_input", "issue"),
        [
            ({"command": "gh issue comment 17 --body done"}, 17),
            ({"command": ["gh", "issue", "comment", "9", "-R", "a/b", "-b", "x"]}, 9),
            ({"command": "ls"}, None),
        ],
    )
    def test_comment_actions(self, tool_input, issue):
        record, actions = hook.derive(
            _payload("PreToolUse", tool_name="Bash", tool_input=tool_input), None
        )
        assert record["state"] == "busy"  # a tool is about to run
        assert (actions[0]["issue"] if actions else None) == issue

    def test_unknown_events_write_nothing(self):
        assert hook.derive(_payload("SubagentStart"), None) == (None, None)


class TestAHookNeverFailsTheCli:
    def test_an_unexpected_payload_shape_exits_zero(self, tmp_path):
        payload = _payload("PreToolUse", tool_name="Bash", tool_input=["not", "a", "dict"])
        with patch.object(bb.sys, "stdin", io.StringIO(json.dumps(payload))):
            assert hook.main(["--state-dir", str(tmp_path), "--agent", "cx"]) == 0

    def test_a_prompt_that_is_not_a_string_exits_zero(self, tmp_path):
        payload = _payload("UserPromptSubmit", prompt={"weird": True})
        with patch.object(bb.sys, "stdin", io.StringIO(json.dumps(payload))):
            assert hook.main(["--state-dir", str(tmp_path), "--agent", "cx"]) == 0


class TestMainWritesStateTheBackboneReads:
    def test_roundtrip_with_file_reader(self, tmp_path):
        payload = _payload("Stop", last_assistant_message="Done.")
        with patch.object(bb.sys, "stdin", io.StringIO(json.dumps(payload))):
            assert hook.main(["--state-dir", str(tmp_path), "--agent", "cx"]) == 0
        snapshot = read_state_file(tmp_path, "cx")
        assert snapshot.state == AgentState.IDLE
        assert snapshot.session_id == "01a0-codex"
        assert snapshot.last_message == "Done."
        assert snapshot.event == "Stop"
        assert "event Stop" in snapshot.evidence[0]


class TestActionsAreLoggedBeforeAndAfter:
    def test_post_tool_use_logs_the_same_shell_actions(self):
        payload = _payload(
            "PostToolUse",
            tool_name="Bash",
            tool_input={"command": "gh issue comment 5 -b ok"},
            tool_response={"exit_code": 0, "output": "created"},
        )
        record, actions = hook.derive(payload, None)
        assert record is None and actions and actions[0]["issue"] == 5
