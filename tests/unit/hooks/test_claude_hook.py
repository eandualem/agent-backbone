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


@pytest.fixture(autouse=True)
def _no_backbone_env(monkeypatch):
    # In a backbone-started session BACKBONE_STATE_DIR points at the real
    # state dir and would win over --state-dir; the tests must never touch it.
    monkeypatch.delenv("BACKBONE_STATE_DIR", raising=False)


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
        assert record["state"] == hook.STATE_WAITING
        assert record["reason"] == hook.REASON_PERMISSION

    def test_notification_idle_prompt(self):
        record, _ = hook.derive(
            _payload("Notification", message="Claude is waiting for your input"), None
        )
        assert record["state"] == hook.STATE_IDLE

    @pytest.mark.parametrize(
        ("kind", "expected_state", "expected_reason"),
        [
            ("quota_auto_resume_armed", "blocked", "quota"),
            ("quota_auto_resume_cancelled", "blocked", "quota"),
            ("quota_auto_resume_fired", hook.STATE_BUSY, None),
            ("quota_auto_resume_stale_resumed", hook.STATE_BUSY, None),
        ],
    )
    def test_quota_notifications(self, kind, expected_state, expected_reason):
        record, _ = hook.derive(
            _payload("Notification", notification_type=kind, message="Resumes at 3:00 PM"), None
        )
        assert record["state"] == expected_state
        assert record["reason"] == expected_reason
        if expected_state == "blocked":
            assert record["detail"] == "Resumes at 3:00 PM"

    def test_notification_other_is_ignored(self):
        assert hook.derive(_payload("Notification", message="something else"), None) == (None, None)

    def test_plan_mode_enter_and_exit(self):
        plan = "# Add caching\n\n1. do things"
        record, _ = hook.derive(
            _payload("PreToolUse", tool_name="ExitPlanMode", tool_input={"plan": plan}), None
        )
        assert record["state"] == hook.STATE_WAITING
        assert record["reason"] == hook.REASON_PLAN
        assert record["plan_title"] == "Add caching"
        assert record["plan_text"] == plan

        record, _ = hook.derive(_payload("PostToolUse", tool_name="ExitPlanMode"), record)
        assert record["state"] == hook.STATE_BUSY
        assert record["reason"] is None

    def test_ask_user_question_is_waiting_with_reason(self):
        record, _ = hook.derive(_payload("PreToolUse", tool_name="AskUserQuestion"), None)
        assert record["state"] == hook.STATE_WAITING
        assert record["reason"] == hook.REASON_QUESTION

    def test_issue_number_captured_from_prompt(self):
        record, _ = hook.derive(
            _payload("UserPromptSubmit", prompt="New issue targeting you: acme/app#42 ..."), None
        )
        assert record["issue"] == 42
        assert record["repo"] == "acme/app"
        # Preserved across later events
        later, _ = hook.derive(_payload("Stop"), record)
        assert later["issue"] == 42 and later["repo"] == "acme/app"

    def test_stop_records_the_last_message_and_the_event(self):
        record, _ = hook.derive(_payload("Stop", last_assistant_message="Shipped."), None)
        assert record["last_message"] == "Shipped."
        assert record["event"] == "Stop" and record["session_id"] == "abc"

    def test_started_at_is_stable(self):
        first, _ = hook.derive(_payload("SessionStart"), None)
        later, _ = hook.derive(_payload("Stop"), first)
        assert later["started_at"] == first["started_at"]

    @pytest.mark.parametrize(
        ("tool", "tool_input", "issue"),
        [
            ("Bash", {"command": "gh issue comment 17 --body 'done'"}, 17),
            ("Bash", {"command": "gh issue comment --repo a/b 9 -b x"}, 9),
            ("Bash", {"command": "gh issue comment 12 -R acme/app -b x"}, 12),
            ("Bash", {"command": "gh pr comment 140 --repo acme/app --body x"}, 140),
            ("mcp__github__add_issue_comment", {"issue_number": 5}, 5),
            ("Bash", {"command": "ls"}, None),
            ("Edit", {"file_path": "x"}, None),
        ],
    )
    def test_comment_actions(self, tool, tool_input, issue):
        _, action = hook.derive(_payload("PreToolUse", tool_name=tool, tool_input=tool_input), None)
        entries = action if isinstance(action, list) else ([action] if action else [])
        if issue is None:
            assert entries == []
        else:
            assert entries[0]["action"] == "comment" and entries[0]["issue"] == issue


class TestPullRequestActions:
    def test_gh_pr_create_with_explicit_repo_and_head_needs_no_git(self):
        _, actions = hook.derive(
            _payload(
                "PreToolUse",
                tool_name="Bash",
                tool_input={"command": "gh pr create --repo acme/app --head feat/x --title t"},
            ),
            None,
        )
        (action,) = actions
        assert action["action"] == "pull_request"
        assert action["repo"] == "acme/app" and action["branch"] == "feat/x"

    def test_repository_and_branch_come_from_the_checkout(self):
        from agent_backbone.hooks import backbone_state as bb

        answers = {
            ("remote", "get-url", "origin"): "git@github.com:acme/app.git\n",
            ("rev-parse", "--abbrev-ref", "HEAD"): "feat/y\n",
        }

        def _run(argv, **kwargs):
            return type("R", (), {"returncode": 0, "stdout": answers[tuple(argv[3:])]})()

        with patch.object(bb.subprocess, "run", side_effect=_run):
            _, actions = hook.derive(
                _payload("PreToolUse", tool_name="Bash", tool_input={"command": "gh pr create"}),
                None,
            )
        (action,) = actions
        assert action["repo"] == "acme/app" and action["branch"] == "feat/y"
        assert action["head_repo"] == "acme/app"  # same repository: origin is the head

    def test_a_compound_command_logs_both_actions(self):
        _, actions = hook.derive(
            _payload(
                "PreToolUse",
                tool_name="Bash",
                tool_input={"command": "gh issue comment 4 -b x && gh pr create --head b"},
            ),
            None,
        )
        assert [a["action"] for a in actions] == ["comment", "pull_request"]
        assert actions[0]["issue"] == 4 and actions[1]["branch"] == "b"

    def test_a_fork_is_identified_by_its_head_repository(self):
        from agent_backbone.hooks import backbone_state as bb

        answers = {
            ("remote", "get-url", "origin"): "https://github.com/forker/app.git\n",
            ("rev-parse", "--abbrev-ref", "HEAD"): "feat/z\n",
        }

        def _run(argv, **kwargs):
            return type("R", (), {"returncode": 0, "stdout": answers[tuple(argv[3:])]})()

        with patch.object(bb.subprocess, "run", side_effect=_run):
            _, actions = hook.derive(
                _payload(
                    "PreToolUse",
                    tool_name="Bash",
                    tool_input={"command": "gh pr create --repo acme/app --title t"},
                ),
                None,
            )
        (action,) = actions
        assert action["repo"] == "acme/app"  # the base, from --repo
        assert action["head_repo"] == "forker/app"  # the fork, from origin
        assert action["branch"] == "feat/z"

    def test_head_owner_colon_branch_names_the_fork(self):
        _, actions = hook.derive(
            _payload(
                "PreToolUse",
                tool_name="Bash",
                tool_input={"command": "gh pr create --repo acme/app --head forker:feat/q"},
            ),
            None,
        )
        (action,) = actions
        assert action["head_repo"] == "forker/app" and action["branch"] == "feat/q"

    def test_records_carry_the_runtime_that_wrote_them(self, monkeypatch):
        monkeypatch.setenv("BACKBONE_RUNTIME", "claude")
        record, _ = hook.derive(_payload("SessionStart"), None)
        assert record["runtime"] == "claude"


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
        assert snap.state == AgentState.WAITING_FOR_HUMAN
        assert snap.reason == "plan"
        assert snap.plan_title == "P"
        assert open(snap.plan_file).read() == "# P\nbody"

    def test_action_log_appended(self, tmp_path):
        payload = _payload(
            "PreToolUse", tool_name="Bash", tool_input={"command": "gh issue comment 8 -b ok"}
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
