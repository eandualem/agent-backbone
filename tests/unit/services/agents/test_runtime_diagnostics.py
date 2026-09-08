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
