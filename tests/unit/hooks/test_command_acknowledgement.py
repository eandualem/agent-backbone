"""Only an exact successful GitHub publication receipt acknowledges work."""

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
        (
            claude_hook,
            {
                "stdout": "https://github.com/acme/app/issues/5#issuecomment-123",
                "stderr": "",
                "interrupted": False,
            },
        ),
        (
            codex_hook,
            {"exit_code": 0, "output": "https://github.com/acme/app/issues/5#issuecomment-123"},
        ),
        (
            codex_hook,
            {
                "metadata": {"exit_code": 0},
                "output": "https://github.com/acme/app/issues/5#issuecomment-123",
            },
        ),
        (
            codex_hook,
            "Wall time: 0.1 seconds\nProcess exited with code 0\nFinal output:\nhttps://github.com/acme/app/issues/5#issuecomment-123",
        ),
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


def test_attached_files_do_not_hide_the_action():
    # gh 2.99+ uploads an image or a video with --attach <file> (#434).
    (comment,) = bb.shell_actions("gh pr comment 6 -R acme/app -b done --attach a.mp4", None, 1)
    assert comment["issue"] == 6
    (comment,) = bb.shell_actions("gh issue comment 5 -R acme/app --attach=a.png -b x", None, 1)
    assert comment["issue"] == 5
    (created,) = bb.shell_actions(
        "gh pr create -R acme/app -H feat/x -t t --attach a.png --attach=b.mp4", None, 1
    )
    assert (created["action"], created["branch"]) == ("pull_request", "feat/x")


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
    git.assert_called_with(str(tmp_path / "other"), "remote", "get-url", "origin")


@pytest.mark.parametrize("key", ["workdir", "cwd"])
def test_tool_directory_override_scopes_successful_acknowledgment(key):
    def git(cwd, *args):
        if args == ("remote", "get-url", "origin"):
            return "git@github.com:acme/b.git" if cwd == "/repo-b" else "git@github.com:acme/a.git"
        return "branch-b"

    with patch.object(bb, "_git_output", side_effect=git):
        _, actions = codex_hook.derive(
            {
                "hook_event_name": "PostToolUse",
                "cwd": "/repo-a",
                "tool_name": "exec_command",
                "tool_input": {"cmd": "gh issue comment 42 -b done", key: "/repo-b"},
                "tool_response": {"exit_code": 0},
            },
            {},
        )
    assert len(actions) == 1
    assert actions[0]["repo"] == "acme/b"
    assert actions[0]["phase"] == "succeeded"


@pytest.mark.parametrize("override", [None, "", 42, {}])
def test_invalid_workdir_never_acknowledges_in_the_parent_repository(override):
    assert (
        bb.tool_actions("exec_command", {"cmd": COMMAND, "workdir": override}, "/repo-a", 1) == []
    )


@pytest.mark.parametrize("override", ["acme/selected", "github.com/acme/selected"])
def test_gh_repo_override_scopes_comment(monkeypatch, override):
    monkeypatch.setenv("GH_REPO", override)
    monkeypatch.delenv("GH_HOST", raising=False)
    with patch.object(bb, "_git_output", return_value="git@github.com:acme/origin.git"):
        (action,) = bb.shell_actions("gh issue comment 42 -b done", "/repo", 1)
        (explicit,) = bb.shell_actions("gh issue comment 42 -R acme/explicit -b done", "/repo", 1)
    assert action["repo"] == "acme/selected"
    assert explicit["repo"] == "acme/explicit"


def test_gh_default_remote_does_not_acknowledge_origin(monkeypatch, tmp_path):
    monkeypatch.delenv("GH_REPO", raising=False)
    monkeypatch.delenv("GH_HOST", raising=False)

    def git_output(_cwd, *args):
        if args == ("config", "--get-regexp", r"^remote\..*\.gh-resolved$"):
            return "remote.upstream.gh-resolved base"
        if args == ("remote", "get-url", "origin"):
            return "git@github.com:acme/origin.git"
        raise AssertionError(args)

    with patch.object(bb, "_git_output", side_effect=git_output):
        (action,) = bb.shell_actions("gh issue comment 42 -b done", "/repo", 1)
    assert "repo" not in action
    bb.append_action(tmp_path, "ike", {**action, "phase": "succeeded"})
    assert not has_commented_on_issue(42, "ike", tmp_path / "actions.jsonl", repo="acme/origin")


@pytest.mark.parametrize("hook", [claude_hook, codex_hook])
@pytest.mark.parametrize(
    "output", ["created", "https://github.com/other/repo/issues/5#issuecomment-123"]
)
def test_success_without_a_matching_publication_receipt_cannot_acknowledge(hook, output, tmp_path):
    _, actions = hook.derive(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": COMMAND},
            "tool_response": {"exit_code": 0, "stdout": output},
        },
        None,
    )
    for action in actions:
        bb.append_action(tmp_path, "app", action)
    assert not has_commented_on_issue(5, "app", tmp_path / "actions.jsonl", repo="acme/app")


def test_exact_comment_identity_not_nearby_intent_or_forged_tag(tmp_path):
    bb.append_action(
        tmp_path,
        "app",
        {
            "action": "comment",
            "issue": 5,
            "repo": "acme/app",
            "phase": "succeeded",
            "comment_id": 123,
        },
    )
    log = tmp_path / "actions.jsonl"
    assert find_outgoing_comment(5, log, repo="acme/app", comment_id=123) == "app"
    assert find_outgoing_comment(5, log, repo="acme/app", comment_id=124) is None
    assert find_outgoing_comment(5, log, repo="other/repo", comment_id=123) is None


@pytest.mark.parametrize("runtime", ["claude", "codex", "opencode", "gemini"])
def test_exact_publication_receipt_contract(runtime, tmp_path):
    import json
    import os
    import shutil
    import subprocess

    from agent_backbone.hooks import gemini_hook
    from agent_backbone.hooks.install import hook_source

    url = "https://github.com/acme/app/issues/5#issuecomment-123"
    if runtime == "opencode":
        node = shutil.which("node")
        if node is None:
            pytest.skip("Node is required to exercise the shipped plugin")
        plugin = tmp_path / "hook.mjs"
        shutil.copyfile(hook_source("opencode_hook.js"), plugin)
        shutil.copyfile(hook_source("backbone_state.py"), tmp_path / "backbone_state.py")
        script = """
const { AgentBackbone } = await import(process.argv[1]);
const hooks = await AgentBackbone();
await hooks["tool.execute.after"](
  {tool: "bash", args: {command: process.argv[2]}},
  {metadata: {exit: 0}, content: [{type: "text", text: process.argv[3]}]}
);
"""
        subprocess.run(
            [node, "--input-type=module", "-e", script, plugin.as_uri(), COMMAND, url],
            env={**os.environ, "BACKBONE_AGENT": "app", "BACKBONE_STATE_DIR": str(tmp_path)},
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    else:
        hook = {"claude": claude_hook, "codex": codex_hook, "gemini": gemini_hook}[runtime]
        response = {"exit_code": 0, "stdout": url}
        if runtime == "gemini":
            response = {"llmContent": "Output: " + url, "display": {"name": "Shell"}}
        _, actions = hook.derive(
            {
                "hook_event_name": "AfterTool" if runtime == "gemini" else "PostToolUse",
                "tool_name": "run_shell_command" if runtime == "gemini" else "Bash",
                "tool_input": {"command": COMMAND},
                "tool_response": response,
            },
            None,
        )
        for action in actions:
            bb.append_action(tmp_path, "app", action)
    log = tmp_path / "actions.jsonl"
    assert json.loads(log.read_text().splitlines()[-1])["comment_id"] == 123
    assert has_commented_on_issue(5, "app", log, repo="acme/app")
    assert find_outgoing_comment(5, log, repo="acme/app", comment_id=123) == "app"
    assert find_outgoing_comment(5, log, repo="acme/app", comment_id=124) is None


@pytest.mark.parametrize("ambiguous", [False, True])
def test_unscoped_default_repository_binds_only_an_unambiguous_receipt(ambiguous):
    action = {"action": "comment", "issue": 42, "phase": "succeeded"}
    output = "https://github.com/acme/upstream/issues/42#issuecomment-123"
    if ambiguous:
        output += "\nhttps://github.com/acme/other/issues/42#issuecomment-456"
    (bound,) = bb._comment_receipts([action], {"stdout": output}, shell=True)
    if ambiguous:
        assert "repo" not in bound and "comment_id" not in bound
    else:
        assert bound["repo"] == "acme/upstream" and bound["comment_id"] == 123


def test_delayed_event_finds_exact_receipt_beyond_recent_tail(tmp_path):
    import json

    receipt = {
        "session": "worker",
        "action": "comment",
        "phase": "succeeded",
        "repo": "acme/app",
        "issue": 5,
        "comment_id": 123,
        "ts": 1,
    }
    log = tmp_path / "actions.jsonl"
    log.write_text(json.dumps(receipt) + "\n" + (json.dumps({"noise": "x" * 1024}) + "\n") * 100)
    assert find_outgoing_comment(5, log, repo="acme/app", comment_id=123) == "worker"
    assert has_commented_on_issue(5, "worker", log, repo="acme/app")
    assert find_outgoing_comment(5, log, repo="acme/app", comment_id=124) is None


@pytest.mark.parametrize("hook", [claude_hook, codex_hook])
def test_chained_comments_keep_each_exact_publication_receipt(hook):
    _, actions = hook.derive(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": COMMAND + " && " + COMMAND},
            "tool_response": {
                "exit_code": 0,
                "stdout": "https://github.com/acme/app/issues/5#issuecomment-123\n"
                "https://github.com/acme/app/issues/5#issuecomment-124",
            },
        },
        None,
    )
    assert [action["comment_id"] for action in actions] == [123, 124]
