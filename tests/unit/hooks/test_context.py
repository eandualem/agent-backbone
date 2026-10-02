"""The hook-context protocol: the backbone offers, the hook takes, or the prompt claims."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_backbone.hooks import backbone_state as bb
from agent_backbone.hooks import claude_hook, codex_hook
from agent_backbone.hooks.install import hook_source


def test_offer_take_claim_and_clear(tmp_path):
    assert bb.claim_context(tmp_path, "desk", "7") == "missing"
    assert bb.offer_context(tmp_path, "desk", "7", "first")
    assert bb.offer_context(tmp_path, "desk", "8", "second")
    assert bb.claim_context(tmp_path, "desk", "8") == "claimed"
    assert bb.take_context(tmp_path, "desk") == ["first"]
    assert bb.claim_context(tmp_path, "desk", "7") == "taken"
    assert not bb.offer_context(tmp_path, "desk", "7", "again")  # taken already
    bb.clear_context(tmp_path, "desk", "7")
    assert list((tmp_path / "context" / "desk").iterdir()) == []
    assert bb.take_context(tmp_path, "desk") == []
    assert bb.take_context(tmp_path, "nobody") == []


def test_a_repeated_offer_never_revives_one_the_hook_takes_meanwhile(tmp_path):
    """A replay offers a waiting batch again while the hook may be taking it."""
    assert bb.offer_context(tmp_path, "desk", "7", "batch")
    original = Path.exists
    hook = {"armed": True}

    def hook_takes_after_the_first_check(path):
        found = original(path)
        if hook["armed"]:
            hook["armed"] = False
            hook["took"] = bb.take_context(tmp_path, "desk")
        return found

    with patch.object(Path, "exists", hook_takes_after_the_first_check):
        bb.offer_context(tmp_path, "desk", "7", "batch")
    assert hook["took"] == ["batch"]
    assert bb.take_context(tmp_path, "desk") == []
    assert bb.claim_context(tmp_path, "desk", "7") == "taken"


def _run(hook, tmp_path, payload: dict) -> str:
    with (
        patch.object(hook.sys, "stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout", new_callable=io.StringIO) as out,
    ):
        assert hook.main(["--state-dir", str(tmp_path), "--agent", "desk"]) == 0
    return out.getvalue()


_OPENCODE_DRIVER = """
import fs from "node:fs";
const [plugin, steps, reply] = process.argv.slice(1);
const { AgentBackbone } = await import(plugin);
const prompts = [];
const client = { session: {
  prompt: async (request) => {
    prompts.push(request);
    return reply === "error" ? { error: { name: "BadRequest" } } : { data: {} };
  },
  // "child" is a subagent resumed from an earlier run: no session.created for it.
  // "old" is the agent's own session resumed from an earlier run, whose lookup never answers.
  get: ({ path }) => path.id === "old" ? new Promise(() => {}) : Promise.resolve({
    data: { id: path.id, parentID: path.id === "child" ? "s" : undefined },
  }),
} };
const hook = await AgentBackbone({ client, directory: process.cwd() });
const status = (sessionID, type) => hook.event({ event: { type: "session.status",
  properties: { sessionID, status: { type } } } });
const state = `${process.env.BACKBONE_STATE_DIR}/desk.json`;
const outputs = [], marks = [], states = [];
for (const step of JSON.parse(steps)) {
  if (step === "busy" || step === "request") {
    await status("s", "busy");
    if (step === "request") await hook["chat.params"]({ sessionID: "s", agent: "plan", message: {
      agent: "plan", system: "house style",
      model: { providerID: "zen", modelID: "free", variant: "high" } } }, {});
  } else if (step === "tool" || step === "child-tool") {
    const sessionID = step === "tool" ? "s" : "child";
    if (sessionID === "child") await status("child", "busy");
    const output = { title: "read", output: "file text", metadata: {} };
    await hook["tool.execute.after"]({ tool: "read", sessionID, callID: "c" }, output);
    outputs.push(output.output);
  } else if (step === "mcp-tool") {
    const output = { content: [{ type: "text", text: "mcp text" }] };
    await hook["tool.execute.after"]({ tool: "srv_q", sessionID: "s", callID: "c" }, output);
    outputs.push(output.content.map((block) => block.text).join("\\n\\n"));
  } else if (step === "old-busy" || step === "old-idle") {
    await status("old", step === "old-busy" ? "busy" : "idle");
  } else if (step === "pause") {
    await new Promise((resolve) => setTimeout(resolve, 20));
  } else if (step === "interrupt") {
    // Escape in the TUI (live, 1.18.32): the reply is aborted, then the turn ends.
    await hook.event({ event: { type: "session.error",
      properties: { sessionID: "s", error: { name: "MessageAbortedError" } } } });
    await status("s", "idle");
    await hook.event({ event: { type: "session.idle", properties: { sessionID: "s" } } });
  } else if (step === "ask" || step === "reject") {
    // A permission dialog, then Reject (live, 1.18.32): the turn ends.
    const type = step === "ask" ? "permission.asked" : "permission.replied";
    await hook.event({ event: { type, properties: { sessionID: "s", id: "p", requestID: "p" } } });
    if (step === "reject") {
      await status("s", "idle");
      await hook.event({ event: { type: "session.idle", properties: { sessionID: "s" } } });
    }
  } else if (step === "plan" || step === "question") {
    // plan_exit asks whether to build; the question tool asks something else
    // (live, 1.18.32 with OPENCODE_EXPERIMENTAL_PLAN_MODE).
    const tool = step === "plan" ? "plan_exit" : "question";
    await hook["tool.execute.before"]({ tool, sessionID: "s", callID: tool }, { args: {} });
    const question = `Plan at ${process.env.BACKBONE_STATE_DIR}/proposed.md is complete. ` +
      "Would you like to switch to the build agent and start implementing?";
    await hook.event({ event: { type: "question.asked", properties: { id: "q", sessionID: "s",
      questions: [{ question, header: "Build Agent", custom: false, options: [] }],
      tool: { messageID: "m", callID: tool } } } });
  } else if (step === "plan-yes" || step === "plan-no") {
    // "1. Yes" builds on in the same turn; "2. No" ends it in the plan agent.
    await hook.event({ event: { type: "question.replied", properties: { sessionID: "s",
      requestID: "q", answers: [[step === "plan-yes" ? "Yes" : "No"]] } } });
    if (step === "plan-no") {
      await status("s", "idle");
      await hook.event({ event: { type: "session.idle", properties: { sessionID: "s" } } });
    }
  } else if (step === "failed-subtask") {
    await hook["tool.execute.after"]({ tool: "task", sessionID: "s", callID: "c" }, undefined);
  } else {
    const sessionID = step === "idle" ? "s" : "child";
    await status(sessionID, "idle");
    await hook.event({ event: { type: "session.idle", properties: { sessionID } } });
  }
  try {
    const record = JSON.parse(fs.readFileSync(state, "utf8"));
    marks.push(record.prompted_at ?? null);
    states.push(record.state);
  } catch { marks.push(null); states.push(null); }
}
console.log(JSON.stringify({ prompts, outputs, marks, states }));
"""


def _opencode(tmp_path, steps: list[str], *, reply: str = "ok") -> dict:
    """Drive the shipped OpenCode plugin with Node and a stand-in OpenCode client:
    ``busy`` (the agent's session works), ``request`` (and a model request of
    its turn), ``tool`` / ``mcp-tool`` (a tool call ends), ``failed-subtask`` (a
    call ends without a result), ``idle`` (the turn ends), ``interrupt`` (Escape
    ends it), ``ask`` / ``reject`` (a permission dialog, refused), ``plan`` (``plan_exit``
    asks about ``<state_dir>/proposed.md``) / ``plan-yes`` / ``plan-no``, ``question``
    (another tool's question), or ``child-tool`` /
    ``child-idle`` for a resumed subagent, ``old-busy`` / ``old-idle`` for the
    agent's own session resumed while its parent lookup is pending. ``marks``
    and ``states`` are the state record's ``prompted_at`` and state after each step."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to exercise the JavaScript plugin")
    hooks = tmp_path / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "opencode_hook.mjs").write_text(hook_source("opencode_hook.js").read_text())
    done = subprocess.run(
        [node, "--input-type=module", "-e", _OPENCODE_DRIVER]
        + [(hooks / "opencode_hook.mjs").as_uri(), json.dumps(steps), reply],
        env={**os.environ, "BACKBONE_AGENT": "desk", "BACKBONE_STATE_DIR": str(tmp_path)},
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(done.stdout)


RUNTIMES = ("claude", "codex", "opencode")


def _tool_call(runtime: str, tmp_path) -> str:
    """One tool call on ``runtime``: what its hook handed the model, or ``""``."""
    if runtime == "opencode":
        prompts = _opencode(tmp_path, ["request", "tool"])["prompts"]
        return "\n\n".join(part["text"] for p in prompts for part in p["body"]["parts"])
    hook = {"claude": claude_hook, "codex": codex_hook}[runtime]
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "s",
        "tool_name": "Read",
        "tool_response": {"ok": True},
    }
    out = _run(hook, tmp_path, payload)
    if not out:
        return ""
    data = json.loads(out)["hookSpecificOutput"]
    assert data["hookEventName"] == "PostToolUse"
    return data["additionalContext"]


def _prompt(runtime: str, tmp_path, at: float) -> dict:
    """A prompt starts a turn on ``runtime`` (Claude Code, Codex) at ``at``:
    the turn as the backbone names it in an offer."""
    hook = {"claude": claude_hook, "codex": codex_hook}[runtime]
    with patch.object(bb.time, "time", return_value=at):
        _run(hook, tmp_path, {"hook_event_name": "UserPromptSubmit", "session_id": "s"})
    record = bb.read_current(tmp_path, "desk")
    return {"prompted_at": record["prompted_at"], "session_id": record["session_id"]}


def _turn_end(runtime: str, tmp_path, event: str = "Stop") -> None:
    """The turn ends on ``runtime``; nothing can be handed over then."""
    if runtime == "opencode":
        assert _opencode(tmp_path, ["request", "idle"])["prompts"] == []
        return
    hook = {"claude": claude_hook, "codex": codex_hook}[runtime]
    assert _run(hook, tmp_path, {"hook_event_name": event, "session_id": "s"}) == ""


@pytest.mark.parametrize("runtime", RUNTIMES)
def test_hooks_hand_offers_over_after_a_tool_call(tmp_path, monkeypatch, runtime):
    monkeypatch.delenv("BACKBONE_STATE_DIR", raising=False)
    bb.offer_context(tmp_path, "desk", "1", "[via:gmail] mail\n- a1")
    bb.offer_context(tmp_path, "desk", "2", "[via:gmail] mail\n- a2")
    _turn_end(runtime, tmp_path)  # the turn's end cannot carry context; the offers wait
    assert _tool_call(runtime, tmp_path) == "[via:gmail] mail\n- a1\n\n[via:gmail] mail\n- a2"
    assert bb.claim_context(tmp_path, "desk", "1") == "taken"
    assert _tool_call(runtime, tmp_path) == ""


def test_clear_agent_context_drops_every_offer_left_for_a_previous_session(tmp_path):
    bb.offer_context(tmp_path, "desk", "7", "batch")
    (tmp_path / "context" / "desk" / "launch-1").mkdir()
    (tmp_path / "context" / "desk" / "launch-1" / "steer-1.md").write_text("old guidance")
    bb.offer_steer(tmp_path, "desk", "launch-1", "brief-refresh", "old brief")
    bb.offer_steer(tmp_path, "desk", "launch-1", bb.steer_key(2), "old steer", {"prompted_at": 1.0})
    bb.clear_agent_context(tmp_path, "desk")
    assert list((tmp_path / "context" / "desk").iterdir()) == []
    assert bb.take_context(tmp_path, "desk", launch_id="launch-1") == []
    assert bb.claim_context(tmp_path, "desk", "7") == "missing"
    bb.clear_agent_context(tmp_path, "nobody")  # nothing to clear is not an error


def test_clear_agent_context_keeps_what_the_previous_session_took(tmp_path):
    """A taken batch or steer is settled by the backbone, never offered or pasted again."""
    bb.offer_context(tmp_path, "desk", "7", "batch")
    bb.offer_steer(tmp_path, "desk", "launch-1", bb.steer_key(5), "guidance")
    assert bb.take_context(tmp_path, "desk", launch_id="launch-1") == ["batch", "guidance"]
    bb.offer_steer(tmp_path, "desk", "launch-1", bb.steer_key(6), "late guidance")
    bb.retire_steers(tmp_path, "desk", launch_id="launch-1")  # the turn ended first
    bb.clear_agent_context(tmp_path, "desk")
    assert bb.claim_context(tmp_path, "desk", "7") == "taken"
    settled = sorted((key, state) for _, _, key, state, _ in bb.steer_offers(tmp_path, "desk"))
    assert settled == [(5, "taken"), (6, "missed")]
    assert bb.take_context(tmp_path, "desk", launch_id="launch-2") == []


def test_steer_offers_are_scoped_to_the_session_that_they_were_written_for(tmp_path, monkeypatch):
    key = bb.steer_key(12)
    assert key == "steer-00000012"
    assert bb.offer_steer(tmp_path, "desk", "launch-a", key, "guidance a")
    bb.offer_context(tmp_path, "desk", "7", "batch")
    # Another session of the same agent never sees it.
    monkeypatch.setenv("BACKBONE_LAUNCH_ID", "launch-b")
    assert bb.take_context(tmp_path, "desk") == ["batch"]
    bb.clear_context(tmp_path, "desk", "7")
    # The session it was written for takes it after the batches, once.
    monkeypatch.setenv("BACKBONE_LAUNCH_ID", "launch-a")
    bb.offer_context(tmp_path, "desk", "8", "batch 2")
    assert bb.take_context(tmp_path, "desk") == ["batch 2", "guidance a"]
    assert bb.take_context(tmp_path, "desk") == []
    assert not bb.offer_steer(tmp_path, "desk", "launch-a", key, "again")  # taken already
    offers = bb.steer_offers(tmp_path)
    assert [(a, launch, i, state) for a, launch, i, state, _ in offers] == [
        ("desk", "launch-a", 12, "taken")
    ]
    bb.clear_steer(tmp_path, "desk", "launch-a", 12)
    assert bb.steer_offers(tmp_path, "desk") == []


@pytest.mark.parametrize("runtime", RUNTIMES)
def test_hooks_hand_launch_scoped_steers_over_after_a_tool_call(tmp_path, monkeypatch, runtime):
    monkeypatch.delenv("BACKBONE_STATE_DIR", raising=False)
    monkeypatch.setenv("BACKBONE_LAUNCH_ID", "launch-x")
    bb.offer_steer(tmp_path, "desk", "launch-x", bb.steer_key(3), "[via:backbone from:peer] go")
    bb.offer_steer(tmp_path, "desk", "launch-y", bb.steer_key(4), "not for this session")
    assert _tool_call(runtime, tmp_path) == "[via:backbone from:peer] go"
    assert [i for _, _, i, state, _ in bb.steer_offers(tmp_path) if state == "taken"] == [3]


@pytest.mark.parametrize(
    ("runtime", "end"),
    [
        ("claude", "Stop"),
        ("claude", "StopFailure"),
        ("codex", "Stop"),
        ("codex", "Interrupt"),
        ("opencode", "idle"),
    ],
)
def test_a_steer_left_when_the_turn_ends_never_reaches_the_next_task(
    tmp_path, monkeypatch, runtime, end
):
    monkeypatch.delenv("BACKBONE_STATE_DIR", raising=False)
    monkeypatch.setenv("BACKBONE_LAUNCH_ID", "launch-x")
    bb.offer_steer(tmp_path, "desk", "launch-x", bb.steer_key(5), "for the old task")
    _turn_end(runtime, tmp_path, end)
    assert _tool_call(runtime, tmp_path) == ""
    assert [(i, state) for _, _, i, state, _ in bb.steer_offers(tmp_path)] == [(5, "missed")]


@pytest.mark.parametrize("runtime", RUNTIMES)
def test_a_steer_left_by_a_turn_that_ended_unseen_never_reaches_the_next_task(
    tmp_path, monkeypatch, runtime
):
    """Claude Code runs no hook on Escape (#252), Codex none on a provider
    failure (#360): the next prompt starts another turn, and the steer offered
    to the old one is missed. OpenCode sees both ends (``session.idle``)."""
    monkeypatch.delenv("BACKBONE_STATE_DIR", raising=False)
    monkeypatch.setenv("BACKBONE_LAUNCH_ID", "launch-x")
    if runtime == "opencode":
        bb.offer_steer(tmp_path, "desk", "launch-x", bb.steer_key(5), "for the old task")
        assert _opencode(tmp_path, ["request", "interrupt", "request", "tool"])["prompts"] == []
    else:
        turn = _prompt(runtime, tmp_path, at=1000.2)
        bb.offer_steer(tmp_path, "desk", "launch-x", bb.steer_key(5), "for the old task", turn)
        _prompt(runtime, tmp_path, at=1000.7)  # the old turn ended with no hook event
        assert _tool_call(runtime, tmp_path) == ""
    assert [(i, state) for _, _, i, state, _ in bb.steer_offers(tmp_path)] == [(5, "missed")]


@pytest.mark.parametrize("runtime", ("claude", "codex"))
def test_a_steer_is_handed_over_only_within_the_turn_it_was_offered_to(
    tmp_path, monkeypatch, runtime
):
    """Half a second apart, either way round: the turn's prompt decides, not a clock."""
    monkeypatch.delenv("BACKBONE_STATE_DIR", raising=False)
    monkeypatch.setenv("BACKBONE_LAUNCH_ID", "launch-x")
    first = _prompt(runtime, tmp_path, at=1000.2)
    bb.offer_steer(tmp_path, "desk", "launch-x", bb.steer_key(5), "for task one", first)
    second = _prompt(runtime, tmp_path, at=1000.7)
    bb.offer_steer(tmp_path, "desk", "launch-x", bb.steer_key(6), "for task two", second)
    assert _tool_call(runtime, tmp_path) == "for task two"
    offers = sorted((i, state) for _, _, i, state, _ in bb.steer_offers(tmp_path))
    assert offers == [(5, "missed"), (6, "taken")]
    for key in (5, 6):
        bb.clear_steer(tmp_path, "desk", "launch-x", key)
    assert list((tmp_path / "context" / "desk" / "launch-x").iterdir()) == []


@pytest.mark.parametrize("runtime", ("claude", "codex"))
@pytest.mark.parametrize(("session", "handed"), [("s", True), ("s-cleared", False)])
def test_a_new_conversation_never_takes_a_steer_offered_to_the_old_one(
    tmp_path, monkeypatch, runtime, session, handed
):
    """``/clear`` after a turn that ended unseen: the new conversation's
    SessionStart hands context over, but not the old one's steer. Within the
    same conversation (compaction) it still does."""
    monkeypatch.delenv("BACKBONE_STATE_DIR", raising=False)
    monkeypatch.setenv("BACKBONE_LAUNCH_ID", "launch-x")
    turn = _prompt(runtime, tmp_path, at=1000.2)
    bb.offer_steer(tmp_path, "desk", "launch-x", bb.steer_key(5), "for the old task", turn)
    hook = {"claude": claude_hook, "codex": codex_hook}[runtime]
    out = _run(hook, tmp_path, {"hook_event_name": "SessionStart", "session_id": session})
    assert ("for the old task" in out) is handed
    state = "taken" if handed else "missed"
    assert [(i, s) for _, _, i, s, _ in bb.steer_offers(tmp_path)] == [(5, state)]


@pytest.mark.parametrize("runtime", ("claude", "codex"))
def test_a_steer_not_tied_to_a_turn_is_handed_over_as_before(tmp_path, monkeypatch, runtime):
    """No prompt recorded when it was offered, or none now (a record from
    before ``prompted_at``): the next tool call takes it."""
    monkeypatch.delenv("BACKBONE_STATE_DIR", raising=False)
    monkeypatch.setenv("BACKBONE_LAUNCH_ID", "launch-x")
    bb.offer_steer(tmp_path, "desk", "launch-x", bb.steer_key(5), "untied")
    _prompt(runtime, tmp_path, at=1000.2)
    assert _tool_call(runtime, tmp_path) == "untied"
    bb.offer_steer(tmp_path, "desk", "launch-x", bb.steer_key(6), "tied", {"prompted_at": 999.0})
    bb.write_state(tmp_path, "desk", {"state": "busy", "ts": 1000.3, "launch_id": "launch-x"})
    assert _tool_call(runtime, tmp_path) == "tied"


def test_opencode_hands_offers_over_as_a_message_in_the_running_turn(tmp_path, monkeypatch):
    """#276: OpenCode's own path for input typed while it works, keeping what
    drives the turn (agent, model, a request's own system prompt); the tool's
    output is left as it was."""
    monkeypatch.setenv("BACKBONE_LAUNCH_ID", "launch-x")
    bb.offer_steer(tmp_path, "desk", "launch-x", bb.steer_key(3), "[via:backbone from:peer] go")
    result = _opencode(tmp_path, ["request", "tool"])
    assert result["prompts"] == [
        {
            "path": {"id": "s"},
            "body": {
                "noReply": True,
                "agent": "plan",
                "model": {"providerID": "zen", "modelID": "free"},
                "variant": "high",
                "system": "house style",
                "parts": [{"type": "text", "text": "[via:backbone from:peer] go"}],
            },
        }
    ]
    assert result["outputs"] == ["file text"]


@pytest.mark.parametrize(
    ("steps", "reply", "outputs"),
    [
        (["request", "tool"], "error", ["file text\n\n[via:backbone from:peer] go"]),
        (["busy", "tool"], "ok", ["file text\n\n[via:backbone from:peer] go"]),
        (["request", "mcp-tool"], "error", ["mcp text\n\n[via:backbone from:peer] go"]),
    ],
)
def test_opencode_never_drops_a_taken_offer(tmp_path, monkeypatch, steps, reply, outputs):
    """Refused by OpenCode, or no request seen yet to say which agent and model
    run the turn: the taken text rides on the tool's result instead."""
    monkeypatch.setenv("BACKBONE_LAUNCH_ID", "launch-x")
    bb.offer_steer(tmp_path, "desk", "launch-x", bb.steer_key(3), "[via:backbone from:peer] go")
    assert _opencode(tmp_path, steps, reply=reply)["outputs"] == outputs
    assert [(i, state) for _, _, i, state, _ in bb.steer_offers(tmp_path)] == [(3, "taken")]


def test_opencode_offers_wait_for_the_agents_own_session(tmp_path, monkeypatch):
    """A resumed subagent's calls and turn end, or a call without a result,
    neither take nor retire the offers: the root session's next call takes them."""
    monkeypatch.setenv("BACKBONE_LAUNCH_ID", "launch-x")
    bb.offer_steer(tmp_path, "desk", "launch-x", bb.steer_key(3), "[via:backbone from:peer] go")
    steps = ["request", "child-tool", "child-idle", "failed-subtask"]
    result = _opencode(tmp_path, steps)
    assert (result["prompts"], result["outputs"]) == ([], ["file text"])
    assert [(i, state) for _, _, i, state, _ in bb.steer_offers(tmp_path)] == [(3, "offered")]
    assert _tool_call("opencode", tmp_path) == "[via:backbone from:peer] go"


def test_opencode_takes_nothing_once_the_turn_has_ended(tmp_path, monkeypatch):
    """A tool call that ends after its turn did (an interrupt) takes nothing:
    the turn's end retired the steer, and the batch waits for the prompt."""
    monkeypatch.setenv("BACKBONE_LAUNCH_ID", "launch-x")
    bb.offer_context(tmp_path, "desk", "7", "[via:gmail] mail")
    bb.offer_steer(tmp_path, "desk", "launch-x", bb.steer_key(3), "[via:backbone from:peer] go")
    result = _opencode(tmp_path, ["request", "idle", "tool"])
    assert (result["prompts"], result["outputs"]) == ([], ["file text"])
    assert [(i, state) for _, _, i, state, _ in bb.steer_offers(tmp_path)] == [(3, "missed")]
    assert bb.claim_context(tmp_path, "desk", "7") == "claimed"


def test_opencode_ends_a_turn_when_nothing_works_any_more(tmp_path, monkeypatch):
    """The agent's session resumed from an earlier run, its parent not known
    yet: its turn's end still retires the steer. A subagent finishing while
    the turn goes on ends nothing."""
    monkeypatch.setenv("BACKBONE_LAUNCH_ID", "launch-x")
    bb.offer_steer(tmp_path, "desk", "launch-x", bb.steer_key(3), "for the old task")
    result = _opencode(tmp_path, ["old-busy", "old-idle"])
    assert result["states"] == ["busy", "idle"]
    assert [(i, state) for _, _, i, state, _ in bb.steer_offers(tmp_path)] == [(3, "missed")]
    bb.clear_steer(tmp_path, "desk", "launch-x", 3)
    bb.offer_steer(tmp_path, "desk", "launch-x", bb.steer_key(4), "[via:backbone from:peer] go")
    result = _opencode(tmp_path, ["busy", "old-busy", "old-idle"])
    assert result["states"] == ["busy", "busy", "busy"]
    assert [(i, state) for _, _, i, state, _ in bb.steer_offers(tmp_path)] == [(4, "offered")]


def test_opencode_never_carries_a_turns_settings_into_the_next(tmp_path, monkeypatch):
    """A turn whose first tool call comes before its first request (a subtask)
    does not reuse the previous turn's agent and model: the tool's result
    carries the offer."""
    bb.offer_context(tmp_path, "desk", "7", "[via:gmail] mail")
    result = _opencode(tmp_path, ["request", "idle", "busy", "tool"])
    assert (result["prompts"], result["outputs"]) == ([], ["file text\n\n[via:gmail] mail"])


def test_opencode_marks_when_each_turn_starts(tmp_path):
    """The steer check's receipt that another turn began (``prompted_at``, as
    Claude Code and Codex record at ``UserPromptSubmit``): stamped when the
    agent's session starts working, kept through the turn and a subagent's work."""
    steps = ["request", "tool", "request", "child-tool", "child-idle", "idle", "pause", "busy"]
    marks = _opencode(tmp_path, steps)["marks"]
    assert marks[0] is not None
    assert marks[:6] == [marks[0]] * 6
    assert marks[7] > marks[0]


@pytest.mark.parametrize("hook", [claude_hook, codex_hook])
def test_a_session_takes_its_offers_at_session_start(tmp_path, monkeypatch, hook):
    """#273: a resumed session takes its refreshed brief as it starts."""
    monkeypatch.delenv("BACKBONE_STATE_DIR", raising=False)
    monkeypatch.setenv("BACKBONE_LAUNCH_ID", "launch-x")
    bb.offer_steer(tmp_path, "desk", "launch-x", "brief-refresh", "the current brief")
    out = _run(
        hook,
        tmp_path,
        {"hook_event_name": "SessionStart", "session_id": "s", "source": "resume"},
    )
    data = json.loads(out)
    assert data["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert data["hookSpecificOutput"]["additionalContext"] == "the current brief"
    assert _run(hook, tmp_path, {"hook_event_name": "SessionStart", "session_id": "s"}) == ""
