"""An interrupted turn reads idle on every required runtime (#252).

Escape while an agent works, or a refused permission dialog, ends its turn.
Codex runs its ``Interrupt`` hook and OpenCode's plugin sees the session go
idle; Claude Code runs no hook at all, and only its screen says so. The
screens are trimmed live captures (Claude Code 2.1.284, codex-cli 0.157.1,
OpenCode 1.18.32).
"""

import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.hooks import claude_hook, codex_hook
from agent_backbone.services.agents import AgentState, get_agent_state
from agent_backbone.services.runtimes import get_runtime
from tests.unit.hooks.test_context import _opencode, _run

_INF = "agent_backbone.services.agents._inference"
_BOX = "─" * 60


def _notice(text: str, width: int = 60) -> str:
    """A notice as Claude Code draws it: ending two columns short of the input box."""
    return text.rjust(width - 2) + "\n"


CLAUDE_INTERRUPTED = (
    "⏺ line 0\n"
    "  line 1\n"
    "  ⎿ \u00a0Interrupted · What should Claude do instead?\n"
    + _notice("tmux focus-events off · add 'set -g focus-events on'")
    + f"{_BOX}\n"
    "❯ \n"
    f"{_BOX}\n"
    "  ⏸ manual mode on · ? for shortcuts · ← for agents\n"
)

CLAUDE_REFUSED = (
    "❯ create the probe file\n"
    "  Ran 1 shell command\n"
    "  ⎿ \u00a0Interrupted · What should Claude do instead?\n"
    "✻ Churned for 2s · done 10:42 PM\n"
    + _notice("✔ Update installed · Restart to update")
    + f"{_BOX}\n"
    "❯ \n"
    f"{_BOX}\n"
    "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents\n"
)

SCREENS = {
    "claude": CLAUDE_INTERRUPTED,
    "codex": (
        "› wait a minute\n"
        "■ Conversation interrupted - tell the model what to do differently. Something went"
        " wrong? Hit `/feedback` to report the issue.\n"
        "› Ask Codex to do anything\n"
        "  gpt-5.6-sol default · /tmp/work\n"
        "  ? for shortcuts\n"
    ),
    "opencode": (
        "  ┃  Build · Stub main Local stub\n"
        "  ╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
        "   /tmp/work        tab agents  ctrl+p commands    • OpenCode 1.18.32\n"
    ),
}


@pytest.fixture(autouse=True)
def _no_live_agent(monkeypatch):
    """A live session's own variables would send the hooks' records to its state directory."""
    for name in ("BACKBONE_STATE_DIR", "BACKBONE_AGENT", "BACKBONE_RUNTIME", "BACKBONE_LAUNCH_ID"):
        monkeypatch.delenv(name, raising=False)


def _hook(runtime, tmp_path, event, **payload):
    hook = {"claude": claude_hook, "codex": codex_hook}[runtime]
    _run(hook, tmp_path, {"hook_event_name": event, "session_id": "s", **payload})


def _interrupted_turn(runtime, tmp_path, refused_dialog):
    """A turn starts (and asks for a permission), then the person presses Escape."""
    if runtime == "opencode":
        _opencode(tmp_path, ["busy", "ask", "reject"] if refused_dialog else ["busy", "interrupt"])
        return
    _hook(runtime, tmp_path, "UserPromptSubmit", prompt="work")
    if refused_dialog and runtime == "claude":
        _hook(
            runtime,
            tmp_path,
            "Notification",
            message="Claude needs your permission",
            notification_type="permission_prompt",
        )
    elif refused_dialog:
        _hook(runtime, tmp_path, "PermissionRequest", tool_name="Bash")
    if runtime == "codex":
        _hook(runtime, tmp_path, "Interrupt")


def _age(tmp_path, seconds):
    record = json.loads((tmp_path / "desk.json").read_text())
    record["ts"] -= seconds
    (tmp_path / "desk.json").write_text(json.dumps(record))


@pytest.mark.parametrize("refused_dialog", [False, True])
@pytest.mark.parametrize("runtime", ["claude", "codex", "opencode"])
async def test_an_interrupted_turn_reads_idle(tmp_path, runtime, refused_dialog):
    _interrupted_turn(runtime, tmp_path, refused_dialog)
    _age(tmp_path, 10)  # read ten seconds later, well before the hook state goes stale
    snapshot = await get_agent_state(
        tmp_path, "desk", runtime_hint=runtime, pane_content=SCREENS[runtime]
    )
    assert snapshot.state == AgentState.IDLE, snapshot.evidence
    assert snapshot.reason is None


# A tall pane pads the space above the input box; a narrow one wraps the
# line to its text column, where a notice may start too.
CLAUDE_PADDED = CLAUDE_INTERRUPTED.replace(f"{_BOX}\n", "\n" * 50 + f"{_BOX}\n", 1)
CLAUDE_NARROW = (
    "  ⎿ \u00a0Interrupted · What should Claude do\n"
    "     instead?\n"
    + _notice("✔ Update installed · Restart to update", width=45)
    + f"{_BOX[:45]}\n"
    "❯ \n"
    f"{_BOX[:45]}\n"
    "  ⏸ manual mode on · ? for shortcuts\n"
)


@pytest.mark.parametrize(
    "pane",
    [CLAUDE_INTERRUPTED, CLAUDE_REFUSED, CLAUDE_PADDED, CLAUDE_NARROW],
    ids=["escape", "refused", "padded", "narrow"],
)
def test_claude_code_shows_the_interrupt_as_its_latest_output(pane):
    assert get_runtime("claude").detect_interrupted(pane)


@pytest.mark.parametrize(
    "after",
    [
        # A newer turn below the interrupt, finished or still running.
        "❯ next task\n⏺ Done.\n",
        "❯ next task\n✳ Harmonizing… (3s · ↓ 10 tokens)\n",
        # A dialog of that newer turn.
        " Do you want to proceed?\n ❯ 1. Yes\n   2. No\n Esc to cancel · Tab to amend\n",
        # Indented output that is neither the interrupt's wrap nor a notice,
        # even where it ends as a notice would.
        "    newer output\n",
        "     " + "x" * 53 + "\n",
    ],
    ids=["finished", "running", "dialog", "indented", "at-notice-edge"],
)
def test_an_older_interrupt_is_history(after):
    old = CLAUDE_REFUSED.split("✻", 1)[0]
    pane = f"{old}{after}{_BOX}\n❯ \n{_BOX}\n  ⏸ manual mode on · ? for shortcuts\n"
    assert not get_runtime("claude").detect_interrupted(pane)


def _busy(tmp_path, age, **record):
    (tmp_path / "desk.json").write_text(
        json.dumps({"state": "busy", "ts": time.time() - age, **record})
    )


async def test_the_interrupt_waits_for_a_newly_submitted_prompt_to_be_drawn(tmp_path):
    _busy(tmp_path, 1)
    snapshot = await get_agent_state(
        tmp_path, "desk", runtime_hint="claude", pane_content=CLAUDE_INTERRUPTED
    )
    assert snapshot.state == AgentState.BUSY


async def test_a_turn_that_starts_while_the_pane_is_read_keeps_its_record(tmp_path):
    _busy(tmp_path, 10)

    async def new_turn_meanwhile(session):
        _busy(tmp_path, 0)  # its hook records the prompt before the screen redraws
        return CLAUDE_INTERRUPTED

    with patch(f"{_INF}.capture_pane", new_turn_meanwhile):
        snapshot = await get_agent_state(tmp_path, "desk", runtime_hint="claude")
    assert snapshot.state == AgentState.BUSY


async def test_the_runtime_named_by_the_hook_record_is_enough(tmp_path):
    _busy(tmp_path, 10, runtime="claude")
    with patch(f"{_INF}.capture_pane", AsyncMock(return_value=CLAUDE_INTERRUPTED)):
        snapshot = await get_agent_state(tmp_path, "desk")
    assert snapshot.state == AgentState.IDLE
    assert snapshot.source == "pull"
    assert "the interrupt on screen beats the hook's 'busy'" in snapshot.evidence


async def test_runtimes_whose_hooks_report_interrupts_are_not_read_while_busy(tmp_path):
    _busy(tmp_path, 10)
    with patch(f"{_INF}.capture_pane", AsyncMock(return_value=SCREENS["codex"])) as capture:
        snapshot = await get_agent_state(tmp_path, "desk", runtime_hint="codex")
    assert snapshot.state == AgentState.BUSY
    capture.assert_not_called()
