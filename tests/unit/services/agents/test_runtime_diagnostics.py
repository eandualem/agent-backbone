"""An observer can retain runtime errors without weakening hook authority."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.services.agents import get_agent_state, write_state_file
from agent_backbone.services.agents.models import AgentState

PANE = (Path(__file__).parents[3] / "fixtures" / "codex-model-error.txt").read_text()


@pytest.mark.parametrize("state", ["busy", "idle", "blocked", "waiting_for_human"])
async def test_supplied_pane_attaches_error_without_overriding_fresh_hook(tmp_path, state):
    write_state_file(tmp_path, "app", {"state": state, "ts": time.time(), "runtime": "codex"})
    with patch("agent_backbone.services.agents._inference.capture_pane", AsyncMock()) as capture:
        snapshot = await get_agent_state(tmp_path, "app", runtime_hint="codex", pane_content=PANE)
    capture.assert_not_awaited()
    assert snapshot.state == AgentState(state)
    assert {signal.code for signal in snapshot.diagnostics} == {
        "model_account_incompatible",
        "model_changed",
    }
    assert snapshot.diagnostics_observed is True


async def test_unavailable_pane_does_not_mean_error_no_longer_visible(tmp_path):
    write_state_file(tmp_path, "app", {"state": "busy", "ts": time.time()})
    snapshot = await get_agent_state(tmp_path, "app", runtime_hint="codex")
    assert snapshot.state == AgentState.BUSY
    assert snapshot.diagnostics == () and snapshot.diagnostics_observed is False


async def test_empty_supplied_pane_cannot_prove_error_no_longer_visible(tmp_path):
    snapshot = await get_agent_state(tmp_path, "app", runtime_hint="codex", pane_content="")
    assert snapshot.diagnostics == () and snapshot.diagnostics_observed is False


async def test_stale_busy_hook_keeps_observation_when_terminal_is_inconclusive(tmp_path):
    write_state_file(tmp_path, "app", {"state": "busy", "ts": 1})
    pane = PANE.rsplit("›", 1)[0]
    snapshot = await get_agent_state(tmp_path, "app", runtime_hint="codex", pane_content=pane)
    assert snapshot.state == AgentState.BUSY
    assert snapshot.diagnostics_observed
    assert any(signal.code == "model_account_incompatible" for signal in snapshot.diagnostics)


# Each required runtime's own evidence of a failed request and a model change:
# Claude Code's and Codex's banners, and OpenCode's plugin record, since its
# screen shows neither (OpenCode 1.18.32).
_CLAUDE_PANE = (
    "\x1b[38;5;220m\x1b[49m⏺\x1b[39m \x1b[38;5;220mAPI Error: 400 bad request\x1b[39m\n"
    "\n❯ /model sonnet\n"
    "\x1b[38;5;246m\x1b[49m  ⎿  \x1b[39mSet model to \x1b[38;5;153mSonnet 5\x1b[39m for this "
    "session only\n❯ \n"
)
_HOOK_RECORD = {
    "request_error": {"name": "APIError", "status": 400},
    "model_changed": "acme/small",
}
_OPENCODE_PANE = "  ┃  bad request\n     ▣  Build · Small\n  ┃  Build · Small Acme\n"
_OPENCODE_DIALOG = (
    "  ┃  △ Permission required\n"
    '  ┃  $ echo "hi" > hello.txt\n'
    "  ┃   Allow once   Allow always   Reject          ctrl+f fullscreen  ⇆ select  enter confirm\n"
)


@pytest.mark.parametrize("runtime", ["claude", "codex", "opencode"])
async def test_every_required_runtime_records_a_request_error_and_a_model_change(tmp_path, runtime):
    record = {"state": "idle", "ts": time.time(), "runtime": runtime}
    if runtime == "opencode":
        record |= _HOOK_RECORD
    write_state_file(tmp_path, "app", record)
    pane = {
        "claude": _CLAUDE_PANE,
        "codex": PANE.replace("with a ChatGPT account.", "for another reason."),
        "opencode": _OPENCODE_PANE,
    }[runtime]
    snapshot = await get_agent_state(tmp_path, "app", runtime_hint=runtime, pane_content=pane)
    signals = {signal.code: signal for signal in snapshot.diagnostics}
    assert signals.keys() == {"request_error", "model_changed"}
    assert signals["request_error"].http_status == 400
    assert signals["model_changed"].severity == "info"
    assert snapshot.diagnostics_observed


_CODEX_MODEL_SWITCH = (
    "  Switch to gpt-5.6-luna for lower credit usage?\n"
    "› 1. Switch to gpt-5.6-luna\n"
    "  2. Keep current model\n"
    "  Press enter to confirm or esc to go back\n"
)


@pytest.mark.parametrize(
    ("runtime", "state", "ts", "pane"),
    [
        ("opencode", "idle", 1, _OPENCODE_PANE),
        ("opencode", "idle", time.time() + 60, _OPENCODE_DIALOG),
        ("codex", "waiting_for_human", time.time() + 60, _CODEX_MODEL_SWITCH),
    ],
    ids=["stale hook state", "dialog beats the hook's idle", "choice beats the hook's permission"],
)
async def test_the_hook_record_survives_whatever_the_terminal_decides(
    tmp_path, runtime, state, ts, pane
):
    record = {"state": state, "ts": ts, "runtime": runtime}
    if state == "waiting_for_human":
        record["reason"] = "permission"
    write_state_file(tmp_path, "app", {**record, **_HOOK_RECORD})
    snapshot = await get_agent_state(tmp_path, "app", runtime_hint=runtime, pane_content=pane)
    assert {(signal.code, signal.error_type, signal.model) for signal in snapshot.diagnostics} == {
        ("request_error", "APIError", None),
        ("model_changed", None, "acme/small"),
    }


@pytest.mark.parametrize(
    "record",
    [
        {"request_error": {"name": "private error text", "status": 400}},
        {"request_error": {"name": "APIError", "status": "400"}},
        {"request_error": {"name": "APIError", "status": 200}},
        {"request_error": {"name": "APIError", "status": True}},
        {"request_error": "APIError"},
        {"model_changed": "acme/a model"},
        {"model_changed": 7},
    ],
)
async def test_a_malformed_hook_record_is_ignored(tmp_path, record):
    write_state_file(tmp_path, "app", {"state": "idle", "ts": time.time(), **record})
    snapshot = await get_agent_state(
        tmp_path, "app", runtime_hint="opencode", pane_content=_OPENCODE_PANE
    )
    assert snapshot.diagnostics == ()
