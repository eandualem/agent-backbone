"""A plan presented for approval waits for a decision, and is approved or
rejected with the runtime's own keys, on every required runtime (#278).

Claude Code's hook sees its ``ExitPlanMode`` call. Codex's ``Stop`` hook reads
the turn's ``<proposed_plan>`` from its rollout, and Codex asks "Implement this
plan?" in a dialog of its own. The Codex screens are trimmed live captures
(codex-cli 0.157.1); Claude Code's plan is its hook's, so no screen is read.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.hooks import claude_hook, codex_hook
from agent_backbone.services.agents import AgentState, get_agent_state, plan_control, read_plan
from agent_backbone.services.runtimes import get_runtime
from tests.unit.hooks.test_codex_hook import PLAN, codex_rollout
from tests.unit.hooks.test_context import _run
from tests.unit.services.runtimes.test_live_panes import CODEX_PERMISSION_DIALOG

_LAUNCH = "agent_backbone.services.agents.launch"

CODEX_PLAN_DIALOG = (
    "› [plan] add a probe file\n"
    "\n"
    "• Proposed Plan\n"
    "\n"
    "  # Add the probe file\n"
    "\n"
    "  1. Create probe-278.txt.\n"
    "  2. Check that it exists.\n"
    "\n"
    "• Here is the plan.\n"
    "\n"
    "  5:11 AM\n"
    "\n"
    "  Implement this plan?\n"
    "\n"
    "› 1. Yes, implement this plan          Switch to Default and start coding\n"
    "  2. Yes, clear context and implement  Fresh thread with this plan\n"
    "  3. No, stay in Plan mode             Continue planning with the model\n"
    "\n"
    "  enter select · esc back\n"
)

# After "3" (or Escape): back at the prompt, still in Plan mode, and no hook ran.
CODEX_STILL_PLANNING = (
    "• Here is the plan.\n"
    "\n"
    "  5:13 AM\n"
    "\n"
    "› Ask Codex to do anything\n"
    "\n"
    "  stub-model medium · /tmp/work                                  Plan mode\n"
)

SCREENS = {"claude": "", "codex": CODEX_PLAN_DIALOG}

KEYS = {
    "claude": {"approve": ("Escape", "[Z"), "reject": ("Escape",)},
    "codex": {"approve": ("1",), "reject": ("3",)},
}

RUNTIMES = tuple(KEYS)


@pytest.fixture(autouse=True)
def _no_live_agent(monkeypatch):
    """A live session's own variables would send the hooks' records to its state directory."""
    for name in ("BACKBONE_STATE_DIR", "BACKBONE_AGENT", "BACKBONE_RUNTIME", "BACKBONE_LAUNCH_ID"):
        monkeypatch.delenv(name, raising=False)


def _present_plan(runtime, tmp_path):
    """The agent finishes planning, as each runtime reports it."""
    if runtime == "claude":
        payload = {"tool_name": "ExitPlanMode", "tool_input": {"plan": PLAN}}
        _run(claude_hook, tmp_path, {"hook_event_name": "PreToolUse", "session_id": "s", **payload})
        return
    reply = f"Here is the plan.\n\n<proposed_plan>\n{PLAN}\n</proposed_plan>\n"
    for event, extra in (
        ("UserPromptSubmit", {"prompt": "[plan] add a probe file"}),
        ("Stop", {"transcript_path": codex_rollout(tmp_path / "rollout.jsonl", reply)}),
    ):
        _run(codex_hook, tmp_path, {"hook_event_name": event, "session_id": "s", "turn_id": "t1",
                                    **extra})  # fmt: skip


def _age(tmp_path, seconds):
    record = json.loads((tmp_path / "desk.json").read_text())
    record["ts"] -= seconds
    (tmp_path / "desk.json").write_text(json.dumps(record))


@pytest.mark.parametrize("runtime", RUNTIMES)
async def test_a_presented_plan_waits_for_a_decision(tmp_path, runtime):
    _present_plan(runtime, tmp_path)
    _age(tmp_path, 10)  # read ten seconds later, well before the hook state goes stale
    snapshot = await get_agent_state(
        tmp_path, "desk", runtime_hint=runtime, pane_content=SCREENS[runtime]
    )
    assert snapshot.is_plan_waiting, snapshot.evidence
    assert snapshot.plan_title == "Add the probe file"
    assert read_plan(tmp_path, snapshot) == PLAN


@pytest.mark.parametrize("action", ["approve", "reject"])
@pytest.mark.parametrize("runtime", RUNTIMES)
async def test_the_decision_is_sent_with_the_runtimes_own_keys(runtime, action):
    sent = []

    async def send_keys(session, key):
        sent.append(key)
        return True

    with (
        patch(f"{_LAUNCH}.session_exists", AsyncMock(return_value=True)),
        patch(f"{_LAUNCH}.capture_pane", AsyncMock(return_value=SCREENS[runtime])),
        patch("agent_backbone.services.runtimes.base.send_keys", send_keys),
    ):
        outcome, _ = await plan_control("desk", action, runtime=runtime)
    assert outcome == {"approve": "approved", "reject": "rejected"}[action]
    assert tuple(sent) == KEYS[runtime][action]


async def test_a_codex_plan_without_its_dialog_on_screen_is_not_pending(tmp_path):
    """A person answered it in the terminal ("3" or Escape run no hook), or a
    reply merely quoted the tag: back at the prompt, the agent is idle."""
    _present_plan("codex", tmp_path)
    _age(tmp_path, 10)
    snapshot = await get_agent_state(
        tmp_path, "desk", runtime_hint="codex", pane_content=CODEX_STILL_PLANNING
    )
    assert (snapshot.state, snapshot.reason) == (AgentState.IDLE, None)
    assert snapshot.plan_title is None and snapshot.plan_file is None
    assert "terminal shows no plan dialog (codex)" in snapshot.evidence


async def test_a_codex_plan_reads_busy_until_its_dialog_is_drawn(tmp_path):
    _present_plan("codex", tmp_path)
    snapshot = await get_agent_state(
        tmp_path, "desk", runtime_hint="codex", pane_content=CODEX_STILL_PLANNING
    )
    assert snapshot.state == AgentState.BUSY  # nothing is typed into a dialog about to open
    assert not snapshot.is_plan_waiting


@pytest.mark.parametrize(
    ("pane", "pending"), [(CODEX_PLAN_DIALOG, True), ("", False)], ids=["dialog", "no-screen"]
)
async def test_a_stale_codex_plan_stands_only_while_its_dialog_shows(tmp_path, pane, pending):
    _present_plan("codex", tmp_path)
    _age(tmp_path, 400)
    snapshot = await get_agent_state(tmp_path, "desk", 300, runtime_hint="codex", pane_content=pane)
    assert snapshot.is_plan_waiting is pending, snapshot.evidence
    if pending:
        assert snapshot.plan_title == "Add the probe file"
        assert read_plan(tmp_path, snapshot) == PLAN


def test_codex_tells_its_plan_dialog_from_a_permission_prompt():
    codex = get_runtime("codex")
    assert codex.detect_plan_dialog(CODEX_PLAN_DIALOG)
    assert not codex.detect_plan_dialog(CODEX_PERMISSION_DIALOG)
    assert not codex.detect_plan_dialog(CODEX_STILL_PLANNING)
