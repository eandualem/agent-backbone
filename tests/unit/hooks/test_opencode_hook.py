"""Exercise the shipped plugin with Node; no OpenCode process is required."""

import json
import os
import shutil
import subprocess

import pytest

from agent_backbone.hooks.install import hook_source
from agent_backbone.services.agents import has_commented_on_issue


def test_state_records_identify_the_runtime_and_session(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to exercise the JavaScript plugin")
    plugin = tmp_path / "hook.mjs"
    plugin.write_text(hook_source("opencode_hook.js").read_text())
    script = """
const { AgentBackbone } = await import(process.argv[1]);
const hook = await AgentBackbone();
await hook.event({event: {type: "session.status", properties: {
    sessionID: "opencode-session", status: {type: "busy"}
}}});
"""
    subprocess.run(
        [node, "--input-type=module", "-e", script, plugin.as_uri()],
        env={**os.environ, "BACKBONE_AGENT": "app", "BACKBONE_STATE_DIR": str(tmp_path)},
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    state = json.loads((tmp_path / "app.json").read_text())
    assert state["runtime"] == "opencode"
    assert state["session_id"] == "opencode-session"


def test_the_running_model_comes_from_a_completed_opencode_reply(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to exercise the JavaScript plugin")
    plugin = tmp_path / "hook.mjs"
    plugin.write_text(hook_source("opencode_hook.js").read_text())
    script = """
const { AgentBackbone } = await import(process.argv[1]);
const hook = await AgentBackbone();
const reply = (sessionID, extra) => hook.event({event: {type: "message.updated", properties: {
    info: {role: "assistant", sessionID, providerID: "acme", modelID: "large", ...extra}
}}});
const created = (info) => hook.event({event: {type: "session.created", properties: {info}}});
await created({id: "root"});
await created({id: "sub", parentID: "root"});
await reply("root", {time: {created: 1, completed: 2}});
await reply("sub", {modelID: "subagent", time: {created: 1, completed: 2}});
await reply("root", {modelID: "streaming", time: {created: 3}});
await reply("root", {modelID: "failed", time: {completed: 4}, error: {name: "APIError"}});
await hook.event({event: {type: "session.idle", properties: {sessionID: "root"}}});
"""
    subprocess.run(
        [node, "--input-type=module", "-e", script, plugin.as_uri()],
        env={**os.environ, "BACKBONE_AGENT": "app", "BACKBONE_STATE_DIR": str(tmp_path)},
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    state = json.loads((tmp_path / "app.json").read_text())
    assert (state["runtime"], state["model"]) == ("opencode", "acme/large")


def test_a_resumed_opencode_session_keeps_its_observed_model(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to exercise the JavaScript plugin")
    plugin = tmp_path / "hook.mjs"
    plugin.write_text(hook_source("opencode_hook.js").read_text())
    state = tmp_path / "app.json"
    saved = {"runtime": "opencode", "session_id": "root", "model": "acme/large"}
    state.write_text(json.dumps(saved))
    script = """
const { AgentBackbone } = await import(process.argv[1]);
const hook = await AgentBackbone();
await hook.event({event: {type: "session.status", properties: {
    sessionID: process.argv[2], status: {type: "busy"}
}}});
"""
    env = {**os.environ, "BACKBONE_AGENT": "app", "BACKBONE_STATE_DIR": str(tmp_path)}
    for session in ("root", "another"):
        subprocess.run(
            [node, "--input-type=module", "-e", script, plugin.as_uri(), session],
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if session == "root":
            assert json.loads(state.read_text())["model"] == "acme/large"
    assert "model" not in json.loads(state.read_text())


def test_plugin_uses_shared_parser_and_acknowledges_only_success(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to exercise the JavaScript plugin")
    plugin = tmp_path / "hook.mjs"
    plugin.write_text(hook_source("opencode_hook.js").read_text())
    shutil.copyfile(hook_source("backbone_state.py"), tmp_path / "backbone_state.py")
    script = """
const { AgentBackbone } = await import(process.argv[1]);
const hook = await AgentBackbone();
const before = hook["tool.execute.before"], after = hook["tool.execute.after"];
const command = "gh issue comment 5 -R acme/app -b done";
let timerFired = false;
setTimeout(() => { timerFired = true; }, 0);
await before({tool: "bash"}, {args: {command: 'echo "gh issue comment 6 -R acme/app"'}});
if (!timerFired) throw new Error("parser blocked the plugin event loop");
await before({tool: "bash"}, {args: {command}});
await after({tool: "bash", args: {command}}, {metadata: {exit: 1}});
await after({tool: "bash", args: {command: "gh issue comment 7 -R acme/app -b done"}},
            {metadata: {exit: 0}});
"""
    subprocess.run(
        [node, "--input-type=module", "-e", script, plugin.as_uri()],
        env={**os.environ, "BACKBONE_AGENT": "app", "BACKBONE_STATE_DIR": str(tmp_path)},
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    log = tmp_path / "actions.jsonl"
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(row["issue"], row["phase"]) for row in rows] == [(5, "intent"), (7, "succeeded")]
    assert not has_commented_on_issue(5, "app", log, repo="acme/app")
    assert has_commented_on_issue(7, "app", log, repo="acme/app")
