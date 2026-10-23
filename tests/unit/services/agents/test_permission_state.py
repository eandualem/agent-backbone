"""A permission hook is a request, not proof of a current human dialog."""

import time
from unittest.mock import patch

import pytest

from agent_backbone.services.agents import AgentState, get_agent_state, write_state_file
from tests.unit.services.runtimes.test_live_panes import (
    CLAUDE_DIALOG_ANSWERED_WITH_TYPED_TEXT,
    CLAUDE_PERMISSION_DIALOG,
    CODEX_DIALOG_ANSWERED,
    CODEX_PERMISSION_DIALOG,
    OPENCODE_DIALOG_ANSWERED,
    OPENCODE_PERMISSION_DIALOG,
)

CASES = [
    ("claude", CLAUDE_PERMISSION_DIALOG, CLAUDE_DIALOG_ANSWERED_WITH_TYPED_TEXT),
    ("codex", CODEX_PERMISSION_DIALOG, CODEX_DIALOG_ANSWERED),
    ("opencode", OPENCODE_PERMISSION_DIALOG, OPENCODE_DIALOG_ANSWERED),
]


def waiting(tmp_path, age=10):
    write_state_file(
        tmp_path,
        "app",
        {
            "state": "waiting_for_human",
            "reason": "permission",
            "ts": time.time() - age,
            "event": "PermissionRequest",
            "session_id": "example",
            "issue": 7,
        },
    )


@pytest.mark.parametrize("runtime,dialog,answered", CASES)
@pytest.mark.parametrize("age", [0, 10, 301])
async def test_current_permission_dialog_stays_waiting(tmp_path, runtime, dialog, answered, age):
    waiting(tmp_path, age)
    state = await get_agent_state(tmp_path, "app", runtime_hint=runtime, pane_content=dialog)
    assert state.state == AgentState.WAITING_FOR_HUMAN and state.reason == "permission"


@pytest.mark.parametrize("runtime,dialog,answered", CASES)
async def test_answered_dialog_does_not_mean_the_turn_ended(tmp_path, runtime, dialog, answered):
    waiting(tmp_path)
    state = await get_agent_state(tmp_path, "app", runtime_hint=runtime, pane_content=answered)
    assert state.state == AgentState.BUSY and state.reason is None
    assert state.current_issue == 7 and state.session_id == "example"
    assert "turn completion is unproven" in " ".join(state.evidence)


@pytest.mark.parametrize("runtime,dialog,answered", CASES)
async def test_old_dialog_does_not_resurrect_stale_wait(tmp_path, runtime, dialog, answered):
    waiting(tmp_path, 301)
    state = await get_agent_state(tmp_path, "app", runtime_hint=runtime, pane_content=answered)
    assert state.state == AgentState.IDLE and state.reason is None


@pytest.mark.parametrize("runtime,dialog,answered", CASES)
@pytest.mark.parametrize("age,pane", [(0, "answered"), (10, "inconclusive")])
async def test_redraw_and_inconclusive_screen_keep_wait(
    tmp_path, runtime, dialog, answered, age, pane
):
    waiting(tmp_path, age)
    state = await get_agent_state(
        tmp_path,
        "app",
        runtime_hint=runtime,
        pane_content=answered if pane == "answered" else "inconclusive",
    )
    assert state.state == AgentState.WAITING_FOR_HUMAN


@pytest.mark.parametrize("runtime,dialog,answered", CASES)
async def test_new_hook_written_during_capture_wins(tmp_path, runtime, dialog, answered):
    waiting(tmp_path)

    async def capture(_):
        waiting(tmp_path, 0)
        return answered

    with patch("agent_backbone.services.agents._inference.capture_pane", new=capture):
        state = await get_agent_state(tmp_path, "app", runtime_hint=runtime)
    assert state.state == AgentState.WAITING_FOR_HUMAN and state.source == "push"


@pytest.mark.parametrize(
    "screen,state,reason",
    [
        ("✗ Request denied for codex to run example\n›", AgentState.BUSY, None),
        ("Working (esc to interrupt)\n›", AgentState.BUSY, None),
        ("■ Selected model is at capacity.\n›", AgentState.BLOCKED, "provider"),
    ],
)
async def test_refusal_work_and_provider_error_are_not_human_waits(tmp_path, screen, state, reason):
    waiting(tmp_path)
    result = await get_agent_state(tmp_path, "app", runtime_hint="codex", pane_content=screen)
    assert (result.state, result.reason) == (state, reason)
