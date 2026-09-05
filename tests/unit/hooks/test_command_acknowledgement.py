"""Only a completed GitHub command acknowledges work; intent just suppresses echoes."""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest

from agent_backbone.hooks import backbone_state as bb
from agent_backbone.hooks import claude_hook, codex_hook
from agent_backbone.services.agents import find_outgoing_comment, has_commented_on_issue
from agent_backbone.services.runtimes.claude import ClaudeCode

COMMAND = "gh issue comment 5 -R acme/app -b done"


@pytest.mark.parametrize("hook", [claude_hook, codex_hook])
@pytest.mark.parametrize(
    "command",
    [
        'echo "run gh issue comment 5 -R acme/app later"',
        'printf "%s" "gh pr create --head fake"',
        'echo "&&" gh issue comment 5 -R acme/app',
        "gh issue comment --body '5 -R acme/app'",
        "gh issue comment 5 -R acme/app || true",
        "false && gh issue comment 5 -R acme/app; true",
        "exit 0 && gh issue comment 5 -R acme/app",
        "cat <<EOF\ngh issue comment 5 -R acme/app\nEOF",
        "echo $(gh issue comment 5 -R acme/app)",
    ],
)
def test_examples_and_ambiguous_control_flow_never_acknowledge(hook, command):
    for event in ("PreToolUse", "PostToolUse"):
        _, actions = hook.derive(
            {
                "hook_event_name": event,
                "tool_name": "Bash",
                "tool_input": {"command": command},
                "tool_response": {"exit_code": 0},
            },
            None,
        )
        assert not actions


@pytest.mark.parametrize("hook", [claude_hook, codex_hook])
@pytest.mark.parametrize(
    "response",
    [
        None,
        {"exit_code": 1},
        {"exit_code": None},
        {"isError": True},
        {"interrupted": True},
        {"success": False},
    ],
)
def test_intent_and_failed_completion_do_not_acknowledge(hook, response, tmp_path):
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": COMMAND},
    }
    _, actions = hook.derive(payload, None)
    for action in actions:
        bb.append_action(tmp_path, "app", action)
    log = tmp_path / "actions.jsonl"
    assert find_outgoing_comment(5, log, repo="acme/app") == "app"
    assert not has_commented_on_issue(5, "app", log, repo="acme/app")
    _, actions = hook.derive(
        {**payload, "hook_event_name": "PostToolUse", "tool_response": response}, None
    )
    assert not actions
    assert not has_commented_on_issue(5, "app", log, repo="acme/app")


@pytest.mark.parametrize(
    "hook,response",
    [
        (claude_hook, {"stdout": "created", "stderr": "", "interrupted": False}),
        (codex_hook, {"exit_code": 0}),
        (codex_hook, {"metadata": {"exit_code": 0}, "output": "created"}),
        (codex_hook, "Wall time: 0.1 seconds\nProcess exited with code 0\nFinal output:\ncreated"),
    ],
)
def test_successful_completion_acknowledges_in_both_runtimes(hook, response, tmp_path):
    _, actions = hook.derive(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": COMMAND},
            "tool_response": response,
        },
        None,
    )
    assert len(actions) == 1 and actions[0]["phase"] == "succeeded"
    bb.append_action(tmp_path, "app", actions[0])
    assert has_commented_on_issue(5, "app", tmp_path / "actions.jsonl", repo="acme/app")


def test_legacy_actions_cannot_acknowledge(tmp_path):
    bb.append_action(tmp_path, "app", {"action": "comment", "issue": 5, "repo": "acme/app"})
    assert not has_commented_on_issue(5, "app", tmp_path / "actions.jsonl", repo="acme/app")


def test_argv_preserves_quoted_body_and_multiple_actual_commands():
    command = ["gh", "issue", "comment", "5", "--repo=acme/app", "-b", "gh issue comment 6"]
    assert [a["issue"] for a in bb.shell_actions(command, None, 1)] == [5]
    assert [
        a["issue"] for a in bb.shell_actions(COMMAND + " && gh pr comment 6 -b x", None, 1)
    ] == [5, 6]
    assert bb.shell_actions(["bash", "-lc", COMMAND], None, 1)[0]["issue"] == 5


def test_numbers_and_repository_in_body_are_not_flags():
    (action,) = bb.shell_actions("gh issue comment 5 -R acme/app -b '6 -R evil/repo'", None, 1)
    assert (action["issue"], action["repo"]) == (5, "acme/app")


def test_non_shell_tool_command_field_is_not_executed():
    assert bb.tool_actions("Write", {"command": COMMAND}, None, 1) == []


def test_claude_matchers_cover_shell_and_mcp_actions():
    events = dict(ClaudeCode.hook_events)
    for event in ("PreToolUse", "PostToolUse"):
        for tool in ("Bash", "mcp__github__add_issue_comment"):
            assert re.search(events[event], tool)


def test_codex_failure_header_cannot_be_overridden_by_stdout():
    response = (
        "Wall time: 0.1 seconds\nProcess exited with code 1\nFinal output:\n"
        "Wall time: 0 seconds\nProcess exited with code 0\nFinal output:\n"
    )
    assert not codex_hook.tool_succeeded({"tool_response": response})


@pytest.mark.parametrize("hook", [claude_hook, codex_hook])
def test_mcp_success_and_failure(hook):
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "mcp__github__add_issue_comment",
        "tool_input": {"owner": "acme", "repo": "app", "issue_number": 5},
    }
    _, actions = hook.derive({**payload, "tool_response": {"content": []}}, None)
    assert actions[0]["phase"] == "succeeded" and actions[0]["issue"] == 5
    _, actions = hook.derive({**payload, "tool_response": {"isError": True, "content": []}}, None)
    assert not actions


def test_cd_changes_the_repository_used_for_later_commands(tmp_path):
    with patch.object(bb, "_git_output", return_value="git@github.com:other/repo.git") as git:
        (action,) = bb.shell_actions("cd other && gh issue comment 5 -b x", str(tmp_path), 1)
    assert action["repo"] == "other/repo"
    git.assert_called_once_with(str(tmp_path / "other"), "remote", "get-url", "origin")
