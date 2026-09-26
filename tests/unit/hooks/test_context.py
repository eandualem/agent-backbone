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
const [plugin, steps, reply] = process.argv.slice(1);
const { AgentBackbone } = await import(plugin);
const prompts = [];
const client = { session: { prompt: async (request) => {
  prompts.push(request);
  return reply === "error" ? { error: { name: "BadRequest" } } : { data: {} };
} } };
const hook = await AgentBackbone({ client, directory: process.cwd() });
const outputs = [];
for (const step of JSON.parse(steps)) {
  if (step === "request") {
    await hook["chat.params"]({ sessionID: "s", agent: "plan", message: {
      agent: "plan", model: { providerID: "zen", modelID: "free", variant: "high" } } }, {});
  } else if (step === "tool") {
    const output = { title: "read", output: "file text", metadata: {} };
    await hook["tool.execute.after"]({ tool: "read", sessionID: "s", callID: "c" }, output);
    outputs.push(output.output);
  } else {
    await hook.event({ event: { type: "session.status",
      properties: { sessionID: "s", status: { type: "idle" } } } });
    await hook.event({ event: { type: "session.idle", properties: { sessionID: "s" } } });
  }
}
console.log(JSON.stringify({ prompts, outputs }));
"""


def _opencode(tmp_path, steps: list[str], *, reply: str = "ok") -> dict:
    """Drive the shipped OpenCode plugin with Node and a stand-in OpenCode client:
    ``request`` (a model request of the turn), ``tool`` (a tool call ends) or
    ``idle`` (the turn ends)."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to exercise the JavaScript plugin")
    hooks = tmp_path / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "opencode_hook.mjs").write_text(hook_source("opencode_hook.js").read_text())
    shutil.copyfile(hook_source("backbone_state.py"), hooks / "backbone_state.py")
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
    [("claude", "Stop"), ("codex", "Stop"), ("codex", "Interrupt"), ("opencode", "idle")],
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


def test_opencode_hands_offers_over_as_a_message_in_the_running_turn(tmp_path, monkeypatch):
    """#276: OpenCode's own path for input typed while it works, keeping the
    turn's agent and model; the tool's output is left as it was."""
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
                "parts": [{"type": "text", "text": "[via:backbone from:peer] go"}],
            },
        }
    ]
    assert result["outputs"] == ["file text"]


@pytest.mark.parametrize(("steps", "reply"), [(["request", "tool"], "error"), (["tool"], "ok")])
def test_opencode_never_drops_a_taken_offer(tmp_path, monkeypatch, steps, reply):
    """Refused by OpenCode, or no request seen yet to say which agent and model
    run the turn: the taken text rides on the tool's output instead."""
    monkeypatch.setenv("BACKBONE_LAUNCH_ID", "launch-x")
    bb.offer_steer(tmp_path, "desk", "launch-x", bb.steer_key(3), "[via:backbone from:peer] go")
    result = _opencode(tmp_path, steps, reply=reply)
    assert result["outputs"] == ["file text\n\n[via:backbone from:peer] go"]
    assert [(i, state) for _, _, i, state, _ in bb.steer_offers(tmp_path)] == [(3, "taken")]


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
