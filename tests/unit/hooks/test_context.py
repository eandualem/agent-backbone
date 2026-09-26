"""The hook-context protocol: the backbone offers, the hook takes, or the prompt claims."""

from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from agent_backbone.hooks import backbone_state as bb
from agent_backbone.hooks import claude_hook, codex_hook


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


def _run(hook, tmp_path, payload: dict) -> str:
    with (
        patch.object(hook.sys, "stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout", new_callable=io.StringIO) as out,
    ):
        assert hook.main(["--state-dir", str(tmp_path), "--agent", "desk"]) == 0
    return out.getvalue()


def test_claude_and_codex_hooks_hand_offers_over_on_post_tool_use(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKBONE_STATE_DIR", raising=False)
    for hook in (claude_hook, codex_hook):
        bb.offer_context(tmp_path, "desk", "1", "[via:gmail] mail\n- a1")
        bb.offer_context(tmp_path, "desk", "2", "[via:gmail] mail\n- a2")
        silent = _run(hook, tmp_path, {"hook_event_name": "Stop", "session_id": "s"})
        assert silent == ""  # Stop cannot carry context; the offers wait
        out = _run(
            hook,
            tmp_path,
            {
                "hook_event_name": "PostToolUse",
                "session_id": "s",
                "tool_name": "Read",
                "tool_response": {"ok": True},
            },
        )
        data = json.loads(out)
        assert data["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
        assert data["hookSpecificOutput"]["additionalContext"] == (
            "[via:gmail] mail\n- a1\n\n[via:gmail] mail\n- a2"
        )
        assert bb.claim_context(tmp_path, "desk", "1") == "taken"
        assert _run(hook, tmp_path, {"hook_event_name": "PostToolUse", "session_id": "s"}) == ""
        for key in ("1", "2"):
            bb.clear_context(tmp_path, "desk", key)


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


def test_hooks_hand_launch_scoped_steers_over_on_post_tool_use(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKBONE_STATE_DIR", raising=False)
    monkeypatch.setenv("BACKBONE_LAUNCH_ID", "launch-x")
    for hook in (claude_hook, codex_hook):
        bb.offer_steer(tmp_path, "desk", "launch-x", bb.steer_key(3), "[via:backbone from:leo] go")
        bb.offer_steer(tmp_path, "desk", "launch-y", bb.steer_key(4), "not for this session")
        out = _run(hook, tmp_path, {"hook_event_name": "PostToolUse", "session_id": "s"})
        data = json.loads(out)
        assert data["hookSpecificOutput"]["additionalContext"] == "[via:backbone from:leo] go"
        assert [i for _, _, i, state, _ in bb.steer_offers(tmp_path) if state == "taken"] == [3]
        bb.clear_steer(tmp_path, "desk", "launch-x", 3)
        bb.clear_steer(tmp_path, "desk", "launch-y", 4)


def test_a_steer_left_when_the_turn_ends_never_reaches_the_next_task(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKBONE_STATE_DIR", raising=False)
    monkeypatch.setenv("BACKBONE_LAUNCH_ID", "launch-x")
    for hook, end in ((claude_hook, "Stop"), (codex_hook, "Stop"), (codex_hook, "Interrupt")):
        bb.offer_steer(tmp_path, "desk", "launch-x", bb.steer_key(5), "for the old task")
        assert _run(hook, tmp_path, {"hook_event_name": end, "session_id": "s"}) == ""
        assert _run(hook, tmp_path, {"hook_event_name": "PostToolUse", "session_id": "s"}) == ""
        assert [(i, state) for _, _, i, state, _ in bb.steer_offers(tmp_path)] == [(5, "missed")]
        bb.clear_steer(tmp_path, "desk", "launch-x", 5)
        assert bb.steer_offers(tmp_path) == []


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
