"""The shipped Gemini CLI hook (stdlib-only script): Gemini events → shared states."""

from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from agent_backbone.hooks import backbone_state as bb
from agent_backbone.hooks import gemini_hook as hook
from agent_backbone.services.agents._file_reader import read_state_file
from agent_backbone.services.agents.models import AgentState


def _payload(event: str, **extra) -> dict:
    return {"hook_event_name": event, "session_id": "d0d9-gemini", "cwd": "/tmp", **extra}


@pytest.fixture(autouse=True)
def _no_backbone_env(monkeypatch):
    monkeypatch.delenv("BACKBONE_STATE_DIR", raising=False)


class TestDerive:
    @pytest.mark.parametrize(
        ("event", "expected"),
        [
            ("SessionStart", bb.STATE_IDLE),
            ("BeforeAgent", bb.STATE_BUSY),
            ("BeforeTool", bb.STATE_BUSY),
            ("AfterAgent", bb.STATE_IDLE),
            ("SessionEnd", bb.STATE_UNKNOWN),
        ],
    )
    def test_states(self, event, expected):
        record, action = hook.derive(_payload(event), None)
        assert record["state"] == expected and action is None
        assert record["event"] == event and record["session_id"] == "d0d9-gemini"

    def test_tool_permission_notification_is_waiting(self):
        record, _ = hook.derive(
            _payload("Notification", notification_type="ToolPermission", message="Allow?"), None
        )
        assert record["state"] == bb.STATE_WAITING and record["reason"] == bb.REASON_PERMISSION

    def test_other_notifications_are_ignored(self):
        payload = _payload("Notification", notification_type="Other")
        assert hook.derive(payload, None) == (None, None)

    def test_after_agent_keeps_the_reply(self):
        record, _ = hook.derive(_payload("AfterAgent", prompt_response="All green."), None)
        assert record["last_message"] == "All green."

    def test_issue_from_prompt(self):
        record, _ = hook.derive(_payload("BeforeAgent", prompt="issue #12 please"), None)
        assert record["issue"] == 12

    def test_shell_comment_action(self):
        _, actions = hook.derive(
            _payload(
                "AfterTool",
                tool_name="run_shell_command",
                tool_input={"command": "gh issue comment 3 -R acme/app -b ok"},
            ),
            None,
        )
        assert actions[0]["issue"] == 3 and actions[0]["repo"] == "acme/app"


class TestMainWritesStateTheBackboneReads:
    def test_roundtrip_with_file_reader(self, tmp_path):
        payload = _payload("Notification", notification_type="ToolPermission")
        with patch.object(bb.sys, "stdin", io.StringIO(json.dumps(payload))):
            assert hook.main(["--state-dir", str(tmp_path), "--agent", "gm"]) == 0
        snapshot = read_state_file(tmp_path, "gm")
        assert snapshot.state == AgentState.WAITING_FOR_HUMAN
        assert snapshot.reason == "permission" and snapshot.event == "Notification"
