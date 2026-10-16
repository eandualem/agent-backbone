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
        env={
            **os.environ,
            "BACKBONE_AGENT": "app",
            "BACKBONE_STATE_DIR": str(tmp_path),
            "BACKBONE_LAUNCH_ID": "replacement-launch",
        },
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    state = json.loads((tmp_path / "app.json").read_text())
    assert state["runtime"] == "opencode"
    assert state["session_id"] == "opencode-session"
    assert state["launch_id"] == "replacement-launch"


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
await reply("root", {time: {created: 1, completed: 2}, finish: "stop"});
await reply("sub", {modelID: "subagent", time: {created: 1, completed: 2}, finish: "stop"});
// A shell command the user ran completes a message no model answered.
await reply("root", {modelID: "shell", time: {created: 3, completed: 4}});
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
           time: {created: 1, completed: 2}, finish: "stop", ...extra}
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
await reply("root", {modelID: "small", finish: undefined});  // a shell command
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
        {"request_error": failed},  # a stopped reply, a shell command, a subagent: no change
        {"model_changed": "acme/small"},  # a reply without an error clears it
        {"request_error": {"name": "UnknownError"}, "model_changed": "acme/small"},
    ]
    assert "PROVIDER" not in state.read_text()


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
    time: {created: 1, completed: 2}, finish: "stop"
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


def test_old_opencode_launch_cannot_overwrite_replacement_state(tmp_path):
    from agent_backbone.services.agents import read_state_file
    from agent_backbone.services.agents._file_reader import (
        clear_starting_marker,
        write_starting_marker,
    )

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to exercise the JavaScript plugin")
    plugin = tmp_path / "hook.mjs"
    plugin.write_text(hook_source("opencode_hook.js").read_text())
    write_starting_marker(tmp_path, "app", 1.0, launch_id="new")
    clear_starting_marker(tmp_path, "app")
    script = """
const { AgentBackbone } = await import(process.argv[1]);
const hook = await AgentBackbone();
process.env.BACKBONE_LAUNCH_ID = "new";
await hook.event({event: {type: "session.status", properties: {
    sessionID: "conversation", status: {type: "busy"}
}}});
// Even an explicitly resumed conversation has a different launch identity.
process.env.BACKBONE_LAUNCH_ID = "old";
await hook.event({event: {type: "session.idle", properties: {sessionID: "conversation"}}});
"""
    subprocess.run(
        [node, "--input-type=module", "-e", script, plugin.as_uri()],
        env={**os.environ, "BACKBONE_AGENT": "app", "BACKBONE_STATE_DIR": str(tmp_path)},
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert json.loads((tmp_path / "app.json").read_text())["state"] == "idle"
    assert read_state_file(tmp_path, "app", current_launch=True).state.value == "busy"


def _run_plugin(tmp_path, script):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to exercise the JavaScript plugin")
    plugin = tmp_path / "hook.mjs"
    plugin.write_text(hook_source("opencode_hook.js").read_text())
    return subprocess.run(
        [node, "--input-type=module", "-e", script, plugin.as_uri()],
        env={**os.environ, "BACKBONE_AGENT": "app", "BACKBONE_STATE_DIR": str(tmp_path)},
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout


_RELOAD = """
import fs from "node:fs";
const disposed = [];
const { AgentBackbone } = await import(process.argv[1]);
const hook = await AgentBackbone({client: {instance: {dispose: async () => disposed.push(1)}}});
const check = globalThis[Symbol.for("agent-backbone.skills-reload")].check;
const marker = `${process.env.BACKBONE_STATE_DIR}/app.skills-reload`;
// A change made after the plugin loaded. Dated explicitly: a coarse filesystem
// clock (Linux before 6.13) can stamp a file just written as older than that.
const change = () => {
  fs.writeFileSync(marker, "");
  const later = new Date(Date.now() + 1000);
  fs.utimesSync(marker, later, later);
};
const status = (type) => hook.event({event: {type: "session.status", properties: {
    sessionID: "root", status: {type}}}});
"""


def test_changed_skills_are_reloaded_between_turns_only(tmp_path):
    out = _run_plugin(
        tmp_path,
        _RELOAD
        + """
await status("busy");
change();
await check();
const duringTurn = [disposed.length, fs.existsSync(marker)];
await status("idle");
await check();
console.log(JSON.stringify({duringTurn, after: [disposed.length, fs.existsSync(marker)]}));
""",
    )
    result = json.loads(out)
    assert result["duringTurn"] == [0, True]  # disposing would abort the turn
    assert result["after"] == [1, False]
    state = json.loads((tmp_path / "app.json").read_text())
    assert (state["event"], state["state"]) == ("skills.reload", "idle")


def test_a_change_older_than_the_process_needs_no_reload(tmp_path):
    out = _run_plugin(
        tmp_path,
        _RELOAD
        + """
fs.writeFileSync(marker, "");
fs.utimesSync(marker, new Date(0), new Date(0));
await check();
console.log(JSON.stringify([disposed.length, fs.existsSync(marker)]));
""",
    )
    assert json.loads(out) == [0, False]


def test_a_failed_reload_does_not_block_the_next_one(tmp_path):
    out = _run_plugin(
        tmp_path,
        _RELOAD
        + """
const state = `${process.env.BACKBONE_STATE_DIR}/app.json`;
fs.mkdirSync(state);  // the state file cannot be written
change();
await check();
fs.rmdirSync(state);
change();
await check();
console.log(JSON.stringify([disposed.length, fs.existsSync(marker)]));
""",
    )
    assert json.loads(out) == [1, False]


def test_a_reload_never_reports_a_turn_begun_meanwhile_idle(tmp_path):
    _run_plugin(
        tmp_path,
        _RELOAD
        + """
// OpenCode builds the plugin again while it disposes the instance, and that
// instance can start the agent's next turn before dispose() returns.
const client = {instance: {dispose: async () => {
  const rebuilt = await AgentBackbone({client});
  await rebuilt.event({event: {type: "session.status", properties: {
      sessionID: "root", status: {type: "busy"}}}});
}}};
await AgentBackbone({client});
const reload = globalThis[Symbol.for("agent-backbone.skills-reload")].check;
change();
await reload();
""",
    )
    state = json.loads((tmp_path / "app.json").read_text())
    assert (state["state"], state["event"]) == ("busy", "session.status")


_SESSIONS = """
import fs from "node:fs";
const { AgentBackbone } = await import(process.argv[1]);
// Each parent lookup waits until the script answers it, or fails it.
const lookups = {};
const client = {session: {get: ({path}) => new Promise((resolve, reject) => {
  lookups[path.id] = {resolve, reject};
})}};
const hook = await AgentBackbone({client});
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const answer = async (id, parentID) => {
  lookups[id].resolve({data: {id, parentID}});
  await settle();
};
const fail = async (id) => { lookups[id].reject(new Error("unavailable")); await settle(); };
const event = (type, properties) => hook.event({event: {type, properties}});
const status = (sessionID, type) => event("session.status", {sessionID, status: {type}});
const idle = async (sessionID) => {
  await status(sessionID, "idle");
  await event("session.idle", {sessionID});
};
const ask = (sessionID, id) => event("permission.asked", {sessionID, id});
const reply = (sessionID, requestID) => event("permission.replied", {sessionID, requestID});
const states = [];
const seen = () => {
  const record = JSON.parse(fs.readFileSync(`${process.env.BACKBONE_STATE_DIR}/app.json`, "utf8"));
  states.push([record.state, record.session_id]);
};
// A steer offered as the backbone offers it: to the turn the state names now.
const context = `${process.env.BACKBONE_STATE_DIR}/context/app`;
const offer = (key, turn) => {
  const { prompted_at, session_id } = JSON.parse(
    fs.readFileSync(`${process.env.BACKBONE_STATE_DIR}/app.json`, "utf8"));
  fs.mkdirSync(context, {recursive: true});
  fs.writeFileSync(`${context}/${key}.turn`, JSON.stringify(turn ?? {prompted_at, session_id}));
  fs.writeFileSync(`${context}/${key}.md`, key);
};
const toolCall = (sessionID) => hook["tool.execute.after"]({sessionID, tool: "read"}, {output: ""});
"""


def _sessions(tmp_path, script) -> list[list[str]]:
    """Drive the plugin through ``script``; the state and session after each ``seen()``."""
    out = _run_plugin(tmp_path, _SESSIONS + script + "console.log(JSON.stringify(states));")
    return json.loads(out)


def test_a_subagent_permission_found_late_does_not_hold_the_turn(tmp_path):
    # A subagent resumed from an earlier run (no session.created) asks before its
    # parent lookup answers, and the agent's own session ends its turn meanwhile.
    states = _sessions(
        tmp_path,
        """
await status("root", "busy");
await status("sub", "busy");
await ask("sub", "p"); seen();
await idle("root"); seen();
await idle("sub");
await answer("sub", "root"); seen();
""",
    )
    assert states == [
        ["waiting_for_human", "sub"],  # unknown yet: it may be the agent's own
        ["waiting_for_human", "sub"],
        ["idle", "root"],  # the agent's session, never the subagent's
    ]


def test_a_subagent_permission_found_during_the_turn_leaves_it_busy(tmp_path):
    states = _sessions(
        tmp_path,
        """
await status("root", "busy");
await ask("sub", "p"); seen();
await answer("sub", "root"); seen();
await reply("sub", "p"); seen();
await idle("root"); seen();
""",
    )
    assert states == [
        ["waiting_for_human", "sub"],
        ["busy", "root"],
        ["busy", "root"],
        ["idle", "root"],
    ]


def test_a_late_turn_end_names_the_agent_session_among_unknown_ones(tmp_path):
    states = _sessions(
        tmp_path,
        """
await event("session.created", {info: {id: "root"}});
await status("root", "busy");
await status("a", "busy");
await status("b", "busy");
await ask("a", "p");
await idle("root");
await idle("b");
await answer("a", "root"); seen();
""",
    )
    assert states == [["idle", "root"]]


def test_a_late_turn_end_leaves_work_begun_since_running(tmp_path):
    states = _sessions(
        tmp_path,
        """
await event("session.created", {info: {id: "a"}});
await status("a", "busy");
await ask("sub", "p");
await idle("a");
await event("session.created", {info: {id: "b"}});
await status("b", "busy");
await answer("sub", "a"); seen();
""",
    )
    assert states == [["busy", "b"]]


def test_a_session_whose_lookup_never_answers_still_reports_its_turn(tmp_path):
    # The agent's own session resumed from an earlier run, its lookup pending.
    states = _sessions(
        tmp_path,
        """
await status("old", "busy"); seen();
await ask("old", "p"); seen();
await reply("old", "p"); seen();
await idle("old"); seen();
""",
    )
    assert states == [
        ["busy", "old"],
        ["waiting_for_human", "old"],
        ["busy", "old"],
        ["idle", "old"],
    ]


def test_a_second_session_after_a_subagent_was_created_still_counts(tmp_path):
    states = _sessions(
        tmp_path,
        """
await event("session.created", {info: {id: "a"}});
await event("session.created", {info: {id: "sub", parentID: "a"}});
await status("b", "busy"); seen();
await answer("b"); seen();
""",
    )
    assert states == [["busy", "b"], ["busy", "b"]]


def test_a_resumed_subagent_found_during_the_turn_gives_the_state_back(tmp_path):
    # A subagent resumed from an earlier run (no session.created) works before
    # its parent lookup answers; once it is known, the state names the agent's
    # own session again, and a steer offered for its turn is still handed over.
    states = _sessions(
        tmp_path,
        """
await event("session.created", {info: {id: "root"}});
await status("root", "busy");
await status("sub", "busy"); seen();
await answer("sub", "root"); seen();
const offers = `${process.env.BACKBONE_STATE_DIR}/context/app`;
fs.mkdirSync(offers, {recursive: true});
fs.writeFileSync(`${offers}/steer-1.turn`, JSON.stringify({session_id: "root"}));
fs.writeFileSync(`${offers}/steer-1.md`, "go");
await hook["tool.execute.after"]({sessionID: "root", tool: "read"}, {output: "done"});
states.push(fs.readdirSync(offers).sort());
""",
    )
    assert states == [
        ["busy", "sub"],  # unknown yet: it may be the agent's own
        ["busy", "root"],
        ["steer-1.taken", "steer-1.turn"],
    ]


def test_a_resumed_session_found_after_its_subagent_gives_the_state_back(tmp_path):
    # The agent's own session was resumed too (no session.created), and its
    # subagent's lookup answers first: once the agent's session is known, the
    # state names it again, and a steer offered for its turn is handed over.
    states = _sessions(
        tmp_path,
        """
await status("root", "busy");
offer("steer-1");
await status("sub", "busy"); seen();
await answer("sub", "root"); seen();
await answer("root"); seen();
await toolCall("root");
states.push(fs.readdirSync(context).sort());
""",
    )
    assert states == [
        ["busy", "sub"],
        ["busy", "sub"],  # whose subagent it is is not known yet
        ["busy", "root"],
        ["steer-1.taken", "steer-1.turn"],
    ]


def test_a_steer_offered_while_a_subagent_was_named_stays_the_turns(tmp_path):
    # Offered while the state named a resumed subagent not known yet: still the
    # agent's turn once the state names its own session again. A steer for an
    # earlier turn that named the same subagent is not.
    states = _sessions(
        tmp_path,
        """
await event("session.created", {info: {id: "root"}});
await status("root", "busy");
await status("sub", "busy");
offer("steer-1");
offer("steer-0", {prompted_at: 1, session_id: "sub"});
await answer("sub", "root"); seen();
await toolCall("root");
states.push(fs.readdirSync(context).sort());
""",
    )
    assert states == [
        ["busy", "root"],
        ["steer-0.missed", "steer-0.turn", "steer-1.taken", "steer-1.turn"],
    ]


def test_a_subagent_found_during_a_reload_leaves_the_reload_to_end(tmp_path):
    # The subagent's parent lookup answers only while the instance is disposed:
    # correcting the session it named must not keep the agent busy afterwards.
    _run_plugin(
        tmp_path,
        """
import fs from "node:fs";
const { AgentBackbone } = await import(process.argv[1]);
const lookups = {};
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const client = {
  session: {get: ({path}) => new Promise((resolve) => { lookups[path.id] = resolve; })},
  instance: {dispose: async () => {
    lookups.sub({data: {id: "sub", parentID: "root"}});
    await settle();
  }},
};
const hook = await AgentBackbone({client});
const event = (type, properties) => hook.event({event: {type, properties}});
const status = (sessionID, type) => event("session.status", {sessionID, status: {type}});
await event("session.created", {info: {id: "root"}});
await status("root", "busy");
await status("sub", "busy");
for (const id of ["root", "sub"]) {
  await status(id, "idle");
  await event("session.idle", {sessionID: id});
}
const marker = `${process.env.BACKBONE_STATE_DIR}/app.skills-reload`;
fs.writeFileSync(marker, "");
const later = new Date(Date.now() + 1000);
fs.utimesSync(marker, later, later);
await globalThis[Symbol.for("agent-backbone.skills-reload")].check();
""",
    )
    state = json.loads((tmp_path / "app.json").read_text())
    assert (state["state"], state["session_id"]) == ("idle", "root")


def test_a_subagent_found_first_never_names_an_earlier_conversation(tmp_path):
    # Session a ran earlier in this process; b is resumed and its subagent's
    # lookup answers before b's: the record waits for b instead of naming a.
    states = _sessions(
        tmp_path,
        """
await event("session.created", {info: {id: "a"}});
await status("a", "busy");
await idle("a");
await status("b", "busy");
await status("sub", "busy");
await answer("sub", "b"); seen();
await answer("b"); seen();
""",
    )
    assert states == [["busy", "sub"], ["busy", "b"]]


def test_a_failed_lookup_keeps_the_session_counted(tmp_path):
    states = _sessions(
        tmp_path,
        """
await status("b", "busy");
await fail("b"); seen();
await event("session.error", {sessionID: "b"}); seen();
""",
    )
    assert states == [["busy", "b"], ["idle", "b"]]
