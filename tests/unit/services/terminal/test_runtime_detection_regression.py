"""Focused regressions for terminal runtime detection."""

from agent_backbone.services.terminal import TerminalRuntime, detect_runtime_from_pane


def test_codex_prompt_wins_over_stale_claude_history() -> None:
    pane = (
        "Previous diagnostic output: Claude Code runtime mismatch\n"
        "More history about opus 4.6 and delivery retries\n\n"
        "\u203a Explain this codebase\n\n"
        "  gpt-5.4 xhigh \u00b7 88% left \u00b7 ~/ws/core/code/WF/agent-backbone"
    )

    assert detect_runtime_from_pane(pane) == TerminalRuntime.CODEX
