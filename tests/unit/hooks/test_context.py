"""The hook-context protocol: the backbone offers, the hook takes, or the prompt claims."""

from __future__ import annotations

import io
import json
from unittest.mock import patch

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
