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


def test_opencode_replies_record_request_errors_and_model_changes(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to exercise the JavaScript plugin")
    plugin = tmp_path / "hook.mjs"
    plugin.write_text(hook_source("opencode_hook.js").read_text())
    script = """
const { AgentBackbone } = await import(process.argv[1]);
const { readFileSync } = await import("node:fs");
const hook = await AgentBackbone();
const reply = (sessionID, extra) => hook.event({event: {type: "message.updated", properties: {
    info: {role: "assistant", sessionID, providerID: "acme", modelID: "large",
           time: {created: 1, completed: 2}, ...extra}
}}});
const seen = [];
const idle = async () => {
    await hook.event({event: {type: "session.idle", properties: {sessionID: "root"}}});
    const { request_error, model_changed } = JSON.parse(readFileSync(process.argv[2], "utf8"));
    seen.push({ request_error, model_changed });
};
const created = (info) => hook.event({event: {type: "session.created", properties: {info}}});
await created({id: "root"});
await created({id: "sub", parentID: "root"});
await reply("root", {});
await idle();
await reply("root", {error: {name: "APIError", data: {
    message: "PROVIDER TEXT", statusCode: 400, responseBody: "PROVIDER BODY"}}});
await idle();
await reply("root", {error: {name: "MessageAbortedError", data: {message: "aborted"}}});
await reply("sub", {modelID: "subagent"});
await idle();
await reply("root", {modelID: "small"});
await idle();
await reply("root", {error: {name: "UnknownError", data: {
    message: "PROVIDER TEXT", statusCode: 0}}});
await idle();
console.log(JSON.stringify(seen));
"""
    state = tmp_path / "app.json"
    result = subprocess.run(
        [node, "--input-type=module", "-e", script, plugin.as_uri(), str(state)],
        env={**os.environ, "BACKBONE_AGENT": "app", "BACKBONE_STATE_DIR": str(tmp_path)},
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    failed = {"name": "APIError", "status": 400}
    assert json.loads(result.stdout) == [
        {},
        {"request_error": failed},
        {"request_error": failed},  # a stopped reply or a subagent's changes nothing
        {"model_changed": "acme/small"},  # a reply without an error clears it
        {"request_error": {"name": "UnknownError"}, "model_changed": "acme/small"},
    ]
    assert "PROVIDER" not in state.read_text()


def test_a_resumed_subagents_failed_reply_is_never_the_agents(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to exercise the JavaScript plugin")
    plugin = tmp_path / "hook.mjs"
    plugin.write_text(hook_source("opencode_hook.js").read_text())
    script = """
const { AgentBackbone } = await import(process.argv[1]);
// A subagent resumed from an earlier run has no session.created: the plugin
// asks OpenCode for its parent, and the answer arrives after the failure.
let answer;
const get = () => new Promise((resolve) => {
    answer = () => resolve({data: {id: "sub", parentID: "root"}});
});
const hook = await AgentBackbone({client: {session: {get}}});
const event = (type, properties) => hook.event({event: {type, properties}});
await event("session.status", {sessionID: "sub", status: {type: "busy"}});
const replied = event("message.updated", {info: {
    role: "assistant", sessionID: "sub", providerID: "acme", modelID: "large",
    time: {created: 1, completed: 2}, error: {name: "APIError", data: {statusCode: 500}}
}});
await event("session.error", {sessionID: "sub"});
answer();
await replied;
"""
    subprocess.run(
        [node, "--input-type=module", "-e", script, plugin.as_uri()],
        env={**os.environ, "BACKBONE_AGENT": "app", "BACKBONE_STATE_DIR": str(tmp_path)},
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert "request_error" not in json.loads((tmp_path / "app.json").read_text())


def test_a_resumed_opencode_session_keeps_its_reply_outcomes(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to exercise the JavaScript plugin")
    plugin = tmp_path / "hook.mjs"
    plugin.write_text(hook_source("opencode_hook.js").read_text())
    state = tmp_path / "app.json"
    failed = {"name": "APIError", "status": 429}
    saved = {"runtime": "opencode", "session_id": "root", "model": "acme/large"}
    state.write_text(json.dumps({**saved, "request_error": failed}))
    script = """
const { AgentBackbone } = await import(process.argv[1]);
const { readFileSync } = await import("node:fs");
const hook = await AgentBackbone();
const seen = [];
const busy = async (sessionID) => {
    await hook.event({event: {type: "session.status", properties: {
        sessionID, status: {type: "busy"}
    }}});
    const { request_error, model_changed } = JSON.parse(readFileSync(process.argv[2], "utf8"));
    seen.push({ request_error, model_changed });
};
await busy("root");
await hook.event({event: {type: "message.updated", properties: {info: {
    role: "assistant", sessionID: "root", providerID: "acme", modelID: "small",
    time: {created: 1, completed: 2}
}}}});
await busy("root");
await busy("another");
console.log(JSON.stringify(seen));
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", script, plugin.as_uri(), str(state)],
        env={**os.environ, "BACKBONE_AGENT": "app", "BACKBONE_STATE_DIR": str(tmp_path)},
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert json.loads(result.stdout) == [
        {"request_error": failed},
        {"model_changed": "acme/small"},  # compared with the model the state file held
        {},
    ]


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
