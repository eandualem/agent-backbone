"""Refusals without a dialog reach the humans once, safely, and are never replayed."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.recent import RecentKeys
from agent_backbone.services.jobs import escalation

REFUSAL = {
    "action": "permission_denied",
    "kind": "classifier",
    "category": "External System Writes",
    "tool": "Bash",
    "summary": "gh issue edit",
    "session": "ike",
}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    # In a backbone-started session BACKBONE_STATE_DIR points at the real state
    # directory, and wins over the hooks' --state-dir.
    monkeypatch.delenv("BACKBONE_STATE_DIR", raising=False)
    monkeypatch.setattr(escalation, "_denial_log_offset", None)
    monkeypatch.setattr(escalation, "_denial_log_inode", None)
    monkeypatch.setattr(escalation, "_denial_watch_started", 0.0)
    monkeypatch.setattr(escalation, "_denials_read", {})
    monkeypatch.setattr(escalation, "_denial_notified", RecentKeys(1800))
    monkeypatch.setattr(escalation, "_denials_unsent", {})


def _append(config, *records, raw: str = ""):
    import time

    config.action_log_path.parent.mkdir(parents=True, exist_ok=True)
    with config.action_log_path.open("a") as log_file:
        for record in records:
            log_file.write(json.dumps({"ts": time.time(), **record}) + "\n")
        log_file.write(raw)


async def _check(config, accepted: bool = True) -> AsyncMock:
    with patch(
        "agent_backbone.services.jobs.escalation.notify_humans", AsyncMock(return_value=accepted)
    ) as notify:
        await escalation.check_permission_denials(config)
    return notify


async def test_refusals_before_the_first_check_are_never_replayed(config):
    _append(config, REFUSAL)
    assert not (await _check(config)).await_count  # the start of the watch
    _append(config, REFUSAL)
    notify = await _check(config)
    assert notify.await_count == 1


async def test_one_notice_names_the_action_and_says_it_cannot_be_approved_here(config):
    await _check(config)
    _append(config, REFUSAL, REFUSAL, {**REFUSAL, "summary": "gh api"})
    notify = await _check(config)
    assert notify.await_count == 2  # the repeat of the same action is folded
    text = notify.await_args_list[0].args[1]
    assert notify.await_args_list[0].kwargs == {"agent": "ike"}  # no buttons
    assert "Refused — ike" in text and "Action: gh issue edit" in text
    assert "auto-mode safety check refused it (External System Writes)" in text
    assert "cannot be approved from here" in text and "/permissions" in text
    assert "does not retry" in text


async def test_unknown_agents_other_actions_and_half_lines_are_skipped(config):
    await _check(config)
    _append(
        config,
        {**REFUSAL, "session": "stranger"},
        {"action": "comment", "session": "ike"},
        raw=json.dumps(REFUSAL)[:20],  # still being written
    )
    assert not (await _check(config)).await_count


async def test_the_first_refusals_in_a_new_log_are_reported(config):
    await _check(config)  # no log yet
    _append(config, REFUSAL)
    assert (await _check(config)).await_count == 1


async def test_a_notice_nobody_accepted_is_tried_again(config):
    await _check(config)
    _append(config, REFUSAL)
    assert (await _check(config, accepted=False)).await_count == 1
    assert (await _check(config)).await_count == 1  # the notice again, not the action
    assert not (await _check(config)).await_count


async def test_refusals_survive_the_log_rotation(config):
    await _check(config)
    _append(config, {"action": "comment", "session": "ike"}, REFUSAL)
    assert (await _check(config)).await_count == 1
    # The prune job keeps the newest lines in a new file; more refusals follow at once,
    # so the file is no smaller than before the rotation.
    from agent_backbone.services.agents import rotate_action_log

    _append(config, *({"action": "comment", "session": "ike"} for _ in range(3)))
    assert rotate_action_log(config.action_log_path, keep_lines=2)
    _append(config, {**REFUSAL, "summary": "gh api"}, {"action": "comment", "session": "ike"})
    notify = await _check(config)
    assert [c.args[1].split("\n")[1] for c in notify.await_args_list] == ["Action: gh api"]


async def test_a_refusal_appended_out_of_timestamp_order_is_not_lost(config):
    import time

    await _check(config)
    now = time.time()
    _append(config, {**REFUSAL, "ts": now + 5, "tool_use_id": "later"})
    assert (await _check(config)).await_count == 1
    _append(config, {**REFUSAL, "ts": now + 1, "tool_use_id": "earlier", "summary": "gh api"})
    assert (await _check(config)).await_count == 1  # an earlier stamp, appended later


async def test_a_rotation_long_after_the_notice_does_not_resend_it(config, monkeypatch):
    from agent_backbone.services.agents import rotate_action_log

    await _check(config)
    _append(config, {**REFUSAL, "tool_use_id": "t1"})
    assert (await _check(config)).await_count == 1
    monkeypatch.setattr(escalation, "_denial_notified", RecentKeys(1800))  # dedup window over
    _append(config, *({"action": "comment", "session": "ike"} for _ in range(3)))
    rotate_action_log(config.action_log_path, keep_lines=3)  # the refusal survives it
    assert not (await _check(config)).await_count


async def test_repeats_during_an_outage_do_not_crowd_out_other_notices(config):
    await _check(config)
    _append(config, *({**REFUSAL} for _ in range(60)), {**REFUSAL, "summary": "gh api"})
    assert (await _check(config, accepted=False)).await_count == 2  # one try per action
    assert len(escalation._denials_unsent) == 2  # one pending notice per distinct action
    notify = await _check(config)
    assert sorted(c.args[1].split("\n")[1] for c in notify.await_args_list) == [
        "Action: gh api",
        "Action: gh issue edit",
    ]


# One behaviour, run against the required three: each runtime's own refusal
# evidence (Claude Code's PermissionDenied event; Codex's screen line after a
# PermissionRequest, from its 0.157.1 TUI snapshot) reaches the humans as one
# notice naming the action safely and the runtime's own way to allow it.
_CODEX_REFUSAL = (
    "⚠ Automatic approval review denied (risk: high): would push private text\n\n"
    "✗ Request denied for codex to run gh issue edit 40 --body-file /tmp/private.md\n"
)


def _refuse_in_claude(state_dir, agent):
    from agent_backbone.hooks import claude_hook

    payload = {
        "hook_event_name": "PermissionDenied",
        "session_id": "s1",
        "tool_name": "Bash",
        "tool_input": {"command": "gh issue edit 40 --body-file /tmp/private.md"},
        "tool_use_id": "toolu_1",
        "reason": "denied by the auto mode classifier. Reason: [External System Writes].",
    }
    _run_hook(claude_hook, state_dir, agent, payload)


def _refuse_in_codex(state_dir, agent):
    from agent_backbone.hooks import codex_hook

    for event, screen in (("PermissionRequest", ""), ("PreToolUse", _CODEX_REFUSAL)):
        payload = {"hook_event_name": event, "session_id": "s1", "tool_name": "Bash"}
        with patch.object(codex_hook, "own_screen", return_value=screen):
            _run_hook(codex_hook, state_dir, agent, payload)


def _refuse_in_opencode(state_dir, agent):
    """Native v2 payloads: only the automatic policy denial becomes an alert."""
    import os
    import shutil
    import subprocess

    from agent_backbone.hooks.install import hook_source

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to exercise the OpenCode plugin")
    plugin_dir = state_dir / "plugin"
    (plugin_dir / "v2").mkdir(parents=True)
    (plugin_dir / "package.json").write_text('{"type":"module"}')
    for name in ("opencode_hook.js", "backbone_state.py"):
        (plugin_dir / name).write_text(hook_source(name).read_text())
    plugin = plugin_dir / "v2" / "index.js"
    plugin.write_text(hook_source("opencode_v2_hook.js").read_text())
    script = r"""
const {default: plugin} = await import(process.argv[1]);
const callbacks = {};
const tools = Object.fromEntries(["shell", "private_custom_tool"].map(id => [id, {
  execute: async (args, context) => {
    if (args.failure) throw args.failure;
    return args.result;
  },
}]));
const cleanup = await plugin.setup({
  location: {directory: process.env.BACKBONE_STATE_DIR},
  session: {hook: async () => {}},
  tool: {
    hook: async (name, fn) => {callbacks[name] = fn;},
    transform: async fn => fn({
      list: () => Object.keys(tools).map(id => ({id})),
      update: (id, fn) => fn(tools[id]),
    }),
  },
  skill: {reload: async () => {}},
  event: {subscribe: async function* () {}},
});
const failure = tag => ({_tag: "Tool.Error", error: {_tag: tag,
  permission: "shell", resources: ["private"], rules: ["private"]}});
const run = async (error, id, tool = "shell", sessionID = "root") => {
  try {
    await tools[tool].execute({failure: error, command: "private"}, {id, sessionID});
    throw new Error("expected original failure");
  } catch (received) {
    if (received !== error) throw new Error("failure changed");
  }
};
// A person's answer and ordinary errors are not automatic policy refusals.
for (const tag of ["Permission.DeclinedError", "Permission.CorrectedError", "OtherError"])
  await run(failure(tag), tag);
const denial = failure("Permission.BlockedError");
await run(denial, "call_one");
await run(denial, "call_one"); // Replays are deduplicated by the consumer.
await run(denial, "call_two", "private_custom_tool", "child");
const result = {content: "original", metadata: {exit: 0}};
if (await tools.shell.execute({result}, {id: "success"}) !== result)
  throw new Error("result changed");
// A failed action-log write must preserve the original native denial too.
const fs = await import("node:fs");
const log = process.env.BACKBONE_STATE_DIR + "/actions.jsonl";
fs.renameSync(log, log + ".saved");
fs.mkdirSync(log);
await run(denial, "logging_failure");
fs.rmdirSync(log);
fs.renameSync(log + ".saved", log);
await cleanup();
"""
    subprocess.run(
        [node, "--input-type=module", "-e", script, str(plugin)],
        env={**os.environ, "BACKBONE_STATE_DIR": str(state_dir), "BACKBONE_AGENT": agent},
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    lines = (state_dir / "actions.jsonl").read_text().splitlines()
    assert len(lines) == 3
    records = [json.loads(line) for line in lines]
    assert all(record["session"] == agent for record in records)
    assert [record["summary"] for record in records] == [
        "a local command",
        "a local command",
        "a tool call",
    ]
    assert "private" not in "".join(lines)


def _run_hook(module, state_dir, agent, payload):
    import io

    with patch.object(module.bb.sys, "stdin", io.StringIO(json.dumps(payload))):
        assert module.main(["--state-dir", str(state_dir), "--agent", agent]) == 0


@pytest.mark.parametrize(
    ("runtime", "refuse", "why", "allow", "summary"),
    [
        (
            "claude",
            _refuse_in_claude,
            " (External System Writes)",
            ", then /permissions",
            "gh issue edit",
        ),
        ("codex", _refuse_in_codex, " (risk: high)", ", then /approve", "gh issue edit"),
        ("opencode", _refuse_in_opencode, "", "", "a local command"),
    ],
)
async def test_a_refusal_without_a_dialog_reaches_the_humans_in_required_runtimes(
    config, monkeypatch, runtime, refuse, why, allow, summary
):
    from dataclasses import replace

    from agent_backbone.services.runtimes import RUNTIMES

    monkeypatch.setenv("TMUX_PANE", "%1")  # inside a pane; every screen read is mocked
    config.agents.specs["ike"] = replace(config.agents.get("ike"), runtime=runtime)
    await _check(config)  # the watch starts
    refuse(config.state_dir, "ike")
    notify = await _check(config)
    calls = notify.await_args_list
    assert len(calls) == (2 if runtime == "opencode" else 1)
    call = calls[0]
    text = call.args[1]
    assert call.kwargs == {"agent": "ike"}  # no buttons: nothing to approve remotely
    assert "Refused — ike" in text and f"Action: {summary}" in text
    assert f"{RUNTIMES[runtime].refusal_check} refused it{why}." in text
    assert f"tmux attach -t ike{allow}" in text and "does not retry" in text
    assert "private" not in text and "40" not in text


async def test_a_refusal_names_the_runtime_that_refused_it(config):
    """An agent moved to another runtime since: the notice still fits the refusal."""
    await _check(config)  # ike is a Claude Code agent here
    _append(config, {**REFUSAL, "runtime": "codex", "kind": "auto_review", "category": ""})
    text = (await _check(config)).await_args.args[1]
    assert "Codex's automatic reviewer refused it." in text and "/approve" in text
