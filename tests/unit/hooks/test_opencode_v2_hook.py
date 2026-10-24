"""Exercise the v2 plugin at native API boundaries without a model call."""

import json
import os
import shutil
import subprocess

import pytest

from agent_backbone.hooks.install import hook_source


@pytest.mark.parametrize("ending", ["interrupted", "failed", "succeeded"])
def test_v2_state_context_and_skill_reload(tmp_path, ending):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to exercise the JavaScript plugin")
    (tmp_path / "package.json").write_text('{"type":"module"}')
    (tmp_path / "opencode_hook.js").write_text(hook_source("opencode_hook.js").read_text())
    (tmp_path / "backbone_state.py").write_text(hook_source("backbone_state.py").read_text())
    plugin = tmp_path / "v2" / "index.js"
    plugin.parent.mkdir()
    plugin.write_text(hook_source("opencode_v2_hook.js").read_text())
    script = r"""
const { default: plugin } = await import(process.argv[1]);
const fs = await import("node:fs");
const path = await import("node:path");
const dir = process.env.BACKBONE_STATE_DIR;
let poll;
globalThis.setInterval = (callback) => { poll = callback; return {unref() {}}; };
const callbacks = {};
const queue = [];
let wake;
let reloaded = 0;
let finishReload;
let holdReload = false;
let reject = false;
let failSubscription = false;
const warnings = [];
const originalWarn = console.warn;
console.warn = (text) => warnings.push(text);
const admitted = [];
const ctx = {
  location: { directory: dir },
  session: {
    get: async ({sessionID}) => ({
      id: sessionID, ...(sessionID === "child" ? {parentID: "root"} : {})
    }),
    synthetic: async (request) => {
      if (reject) throw new Error("admission failed");
      admitted.push(request);
    },
    hook: async (name, fn) => { callbacks[name] = fn; },
  },
  tool: {
    hook: async (name, fn) => { callbacks[name] = fn; },
    transform: async () => {},
  },
  skill: { reload: async () => {
    reloaded++;
    if (holdReload) await new Promise(resolve => { finishReload = resolve; });
  } },
  event: { subscribe: async function* ({signal}) {
    signal.addEventListener("abort", () => wake?.(), {once: true});
    while (!signal.aborted) {
      if (!queue.length) await new Promise(resolve => { wake = resolve; });
      if (signal.aborted) throw new Error("expected abort details");
      if (failSubscription) throw new Error("private failure details");
      yield queue.shift();
    }
  } },
};
const cleanup = await plugin.setup(ctx);
const tick = () => new Promise(resolve => setImmediate(resolve));
const emit = async (type, data) => {
  queue.push({type, data, created: Date.now()});
  wake?.();
  await tick();
  await tick();
};
const state = () => JSON.parse(fs.readFileSync(path.join(dir, "app.json")));
// Both sessions predate this plugin instance, as on an explicit resume.
await emit("session.execution.started", {sessionID: "root"});
await emit("permission.asked", {sessionID: "root", id: "per_one"});
const waiting = state();
await emit("permission.replied", {sessionID: "root", requestID: "per_one", reply: "reject"});
await emit("session.step.started", {
  sessionID: "root", assistantMessageID: "msg_one", model: {providerID: "provider", id: "model"}
});
await emit("session.step.ended", {
  sessionID: "root", assistantMessageID: "msg_one", finish: "tool-calls"
});
await emit("session.execution.succeeded", {sessionID: "child"});
const afterChild = state();
await callbacks.context({sessionID: "root", agent: "build", model: {
  providerID: "provider", id: "model", variant: "high"
}});
const offers = path.join(dir, "context", "app");
fs.mkdirSync(offers, {recursive: true});
fs.writeFileSync(path.join(offers, "1.md"), "first offer");
const tool = () => ({sessionID: "root", tool: "read", id: "call", input: {},
  status: "completed", result: {content: "original result"}});
await callbacks["execute.after"](tool());
const structured = {...tool(), result: {output: {found: 3}}};
await callbacks["execute.after"](structured);
reject = true;
fs.writeFileSync(path.join(offers, "2.md"), "fallback offer");
const fallback = {...tool(), result: {output: {found: 3}}};
await callbacks["execute.after"](fallback);
fs.writeFileSync(path.join(offers, "3.md"), "offer before logging failure");
fs.mkdirSync(path.join(dir, "actions.jsonl"));
const loggingFailure = {...tool(), tool: "shell",
  input: {command: "gh issue comment 1 --repo acme/app --body note"},
  result: {content: "shell result", metadata: {exit: 0}}};
await callbacks["execute.after"](loggingFailure);
const oldPrompt = state().prompted_at;
const steers = path.join(offers, process.env.BACKBONE_LAUNCH_ID);
fs.mkdirSync(steers);
fs.writeFileSync(path.join(steers, "steer-old.md"), "old turn only");
fs.writeFileSync(path.join(steers, "steer-old.turn"), JSON.stringify({
  prompted_at: oldPrompt, session_id: "root"
}));
await emit("permission.asked", {sessionID: "root", id: "per_cancelled"});
await emit("session.execution.__ENDING__", {sessionID: "root"});
await emit("permission.replied", {sessionID: "root", requestID: "per_cancelled"});
const idle = state();
const retired = fs.existsSync(path.join(steers, "steer-old.missed"));
const now = Date.now;
Date.now = () => now() + 1000;
await emit("session.execution.started", {sessionID: "root"});
Date.now = now;
const newPrompt = state().prompted_at;
await emit("session.execution.succeeded", {sessionID: "root"});
// Make one state write fail, then verify the same subscription recovers.
const statePath = path.join(dir, "app.json");
fs.unlinkSync(statePath);
fs.mkdirSync(statePath);
await emit("session.execution.started", {sessionID: "root"});
fs.rmdirSync(statePath);
await emit("session.execution.succeeded", {sessionID: "root"});
const recovered = state();
fs.writeFileSync(path.join(dir, "app.skills-reload"), "");
await poll();
await cleanup();
const afterCleanup = fs.readFileSync(statePath, "utf8");
fs.writeFileSync(path.join(dir, "app.skills-reload"), "");
await poll();
const stopped = fs.readFileSync(statePath, "utf8") === afterCleanup
  && fs.existsSync(path.join(dir, "app.skills-reload")) && reloaded === 1;
const newerCleanup = await plugin.setup(ctx);
await cleanup(); // Old cleanup must not clear the newer callback.
await poll();
// Teardown during a reload must finish its own busy record.
holdReload = true;
fs.writeFileSync(path.join(dir, "app.skills-reload"), "");
const inFlight = poll();
await newerCleanup();
finishReload();
await inFlight;
const afterInFlight = state();
const teardownWarnings = warnings.length;
failSubscription = true;
const brokenCleanup = await plugin.setup(ctx);
await emit("unrelated", {});
await brokenCleanup();
console.warn = originalWarn;
console.log(JSON.stringify({waiting, afterChild, idle, recovered, admitted,
  retired, oldPrompt, newPrompt, warnings, teardownWarnings,
  structured: structured.result, fallback: fallback.result,
  loggingFailure: loggingFailure.result, reloaded, stopped, afterInFlight}));
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", script.replace("__ENDING__", ending), plugin.as_uri()],
        env={
            **os.environ,
            "BACKBONE_AGENT": "app",
            "BACKBONE_STATE_DIR": str(tmp_path),
            "BACKBONE_LAUNCH_ID": "test-launch",
        },
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    actual = json.loads(result.stdout)
    assert (actual["waiting"]["state"], actual["waiting"]["reason"]) == (
        "waiting_for_human",
        "permission",
    )
    assert actual["afterChild"]["state"] == "busy"
    assert actual["afterChild"]["session_id"] == "root"
    assert actual["idle"]["state"] == "idle"
    assert actual["idle"]["model"] == "provider/model"
    assert actual["recovered"]["state"] == "idle"
    assert actual["retired"] is True
    assert actual["newPrompt"] > actual["oldPrompt"]
    assert actual["admitted"] == [
        {"sessionID": "root", "text": "first offer", "delivery": "steer", "resume": False}
    ]
    assert actual["structured"] == {"output": {"found": 3}}
    assert actual["fallback"]["content"] == [
        {"type": "text", "text": '{"found":3}'},
        {"type": "text", "text": "fallback offer"},
    ]
    assert actual["loggingFailure"]["content"] == [
        {"type": "text", "text": "shell result"},
        {"type": "text", "text": "offer before logging failure"},
    ]
    assert actual["teardownWarnings"] == 0
    assert actual["warnings"] == [
        "Backbone: OpenCode event subscription failed; "
        "native state updates stopped until plugin reload."
    ]
    assert actual["stopped"] is True
    assert actual["reloaded"] == 3
    assert actual["afterInFlight"]["state"] == "idle"
