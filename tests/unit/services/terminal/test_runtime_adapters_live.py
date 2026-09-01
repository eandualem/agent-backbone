"""Adapter regressions against panes captured from live CLIs.

The fixtures below are (trimmed) real captures: codex-cli 0.152.0 and
Gemini CLI 0.46.0 running under tmux. When a runtime's UI changes, update
the fixture from a fresh capture, not from memory.
"""

from agent_backbone.services.terminal import TerminalRuntime, detect_runtime_from_pane
from agent_backbone.services.terminal._adapters import get_terminal_adapter

CODEX_TRUST_DIALOG = (
    "> You are in /tmp/rt-test\n"
    "  Do you trust the contents of this directory? Working with untrusted contents"
    " comes with higher risk of prompt injection.\n"
    "› 1. Yes, continue\n"
    "  2. No, quit\n"
    "  Press enter to continue\n"
)

CODEX_IDLE = (
    "╭─────╮\n"
    "│ >_ OpenAI Codex (v0.152.0)\n"
    "│ model:     gpt-5.6-sol   /model to change\n"
    "│ directory: /tmp/rt-test\n"
    "╰─────╯\n"
    "  Tip: Use /copy or press Ctrl+O to copy the latest agent response.\n"
    "› Ask Codex to do anything\n"
    "  gpt-5.6-sol default · /tmp/rt-test\n"
)

CODEX_BUSY = (
    "› Reply with exactly: OK\n"
    "• Working (2s • esc to interrupt)\n"
    "› Ask Codex to do anything\n"
    "  gpt-5.6-sol default · /tmp/rt-test\n"
)

CODEX_TYPED_INPUT = (
    "  Tip: Use /copy or press Ctrl+O to copy the latest agent response.\n"
    "› half typed comman\n"
    "  gpt-5.6-sol default · /tmp/rt-test\n"
)

GEMINI_TRUST_DIALOG = (
    " Gemini CLI v0.46.0\n"
    "│ Do you trust the files in this folder?\n"
    "│ ● 1. Trust folder (rt-test)\n"
    "│   2. Trust parent folder (scratchpad)\n"
    "│   3. Don't trust\n"
)

GEMINI_AUTH_SCREEN = (
    " Gemini CLI v0.46.0\n"
    "│     3. Vertex AI\n"
    "│   Failed to sign in. Message: This client is no longer supported.\n"
    "│   (Use Enter to select)\n"
)


class TestCodexAdapter:
    adapter = get_terminal_adapter("codex")

    def test_trust_dialog_is_waiting_for_human(self):
        assert self.adapter.detect_waiting_for_human(CODEX_TRUST_DIALOG)
        assert not self.adapter.detect_idle(CODEX_TRUST_DIALOG)

    def test_idle_prompt_detected(self):
        assert self.adapter.detect_idle(CODEX_IDLE)
        assert not self.adapter.detect_busy(CODEX_IDLE)
        assert detect_runtime_from_pane(CODEX_IDLE) == TerminalRuntime.CODEX

    def test_idle_placeholder_is_not_pending_input(self):
        assert not self.adapter.prompt_has_pending_input(CODEX_IDLE)

    def test_busy_detected(self):
        assert self.adapter.detect_busy(CODEX_BUSY)
        assert not self.adapter.detect_idle(CODEX_BUSY)

    def test_typed_text_is_pending_input(self):
        assert self.adapter.prompt_has_pending_input(CODEX_TYPED_INPUT)


OPENCODE_FRESH_IDLE = (
    '┃  Ask anything... "Fix a TODO in the codebase"\n'
    "┃\n"
    "┃  Build · Big Pickle OpenCode Zen\n"
    "tab agents  ctrl+p commands\n"
    "/tmp/rt-test:main                              1.18.25\n"
)

OPENCODE_BUSY = (
    "┃  Count slowly from 1 to 30\n"
    "┃  Build · Big Pickle OpenCode Zen\n"
    "   ⬝⬝⬝⬝⬝■■■  esc interrupt                8.6K (4%)  ctrl+p commands  • OpenCode 1.18.25\n"
)

OPENCODE_IDLE_AFTER_RESPONSE = (
    "   DELIVERED\n"
    "   ▣  Build · Big Pickle · 6.9s\n"
    "┃\n"
    "┃  Build · Big Pickle OpenCode Zen\n"
    "   /tmp/rt-test                 8.7K (4%)  ctrl+p commands  • OpenCode 1.18.25\n"
)


class TestOpenCodeAdapter:
    adapter = get_terminal_adapter("opencode")

    def test_fresh_idle_detected(self):
        assert self.adapter.detect_idle(OPENCODE_FRESH_IDLE)
        assert detect_runtime_from_pane(OPENCODE_FRESH_IDLE) == TerminalRuntime.OPENCODE

    def test_busy_detected(self):
        assert self.adapter.detect_busy(OPENCODE_BUSY)
        assert not self.adapter.detect_idle(OPENCODE_BUSY)

    def test_idle_after_first_response_without_placeholder(self):
        # The "Ask anything..." placeholder is gone after the first message;
        # the persistent bottom bar without "esc interrupt" is the idle signal.
        assert self.adapter.detect_idle(OPENCODE_IDLE_AFTER_RESPONSE)
        assert not self.adapter.detect_busy(OPENCODE_IDLE_AFTER_RESPONSE)


class TestGeminiAdapter:
    adapter = get_terminal_adapter("gemini")

    def test_trust_dialog_is_waiting_for_human(self):
        assert self.adapter.detect_waiting_for_human(GEMINI_TRUST_DIALOG)
        assert not self.adapter.detect_idle(GEMINI_TRUST_DIALOG)

    def test_auth_screen_is_waiting_for_human(self):
        assert self.adapter.detect_waiting_for_human(GEMINI_AUTH_SCREEN)
        assert not self.adapter.detect_idle(GEMINI_AUTH_SCREEN)
        assert detect_runtime_from_pane(GEMINI_AUTH_SCREEN) == TerminalRuntime.GEMINI
