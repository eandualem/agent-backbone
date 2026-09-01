"""Adapter regressions against panes captured from live CLIs.

The fixtures below are (trimmed) real captures: codex-cli 0.152.0 and
Gemini CLI 0.46.0 running under tmux. When a runtime's UI changes, update
the fixture from a fresh capture, not from memory.
"""

import pytest

from agent_backbone.services.terminal import TerminalRuntime, detect_runtime_from_pane
from agent_backbone.services.terminal._adapters import get_terminal_adapter


def prompt_has_pending_input(pane: str) -> bool:
    """The adapter for whatever runtime the pane shows, asked about typed input."""
    return get_terminal_adapter(detect_runtime_from_pane(pane)).prompt_has_pending_input(pane)


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


# Permission dialogs captured live on 2026-09-01: Claude Code 2.1.252
# (--permission-mode default), codex-cli 0.152.0 (--sandbox read-only),
# opencode 1.18.25 (permission.bash = "ask").
CLAUDE_PERMISSION_DIALOG = (
    " Bash command\n"
    "   echo hi > hello.txt && cat hello.txt\n"
    '   Create hello.txt containing "hi"\n'
    " Do you want to proceed?\n"
    " ❯ 1. Yes\n"
    "   2. Yes, and always allow access to /tmp/perm/claude from this project\n"
    "   3. Yes, and switch to auto mode · auto mode handles these prompts for you\n"
    "   4. No\n"
    " Esc to cancel · Tab to amend · ctrl+e to explain\n"
)

CODEX_PERMISSION_DIALOG = (
    "• Running printf 'hi\\n' > hello.txt\n"
    "  Would you like to run the following command?\n"
    "  Environment: local\n"
    "  Reason: Allow me to create hello.txt in the workspace using the requested shell command?\n"
    "  $ printf 'hi\\n' > hello.txt\n"
    "› 1. Yes, proceed (y)\n"
    "  2. Yes, and don't ask again for commands that start with `printf 'hi\\n' > hello.txt` (p)\n"
    "  3. No, and tell Codex what to do differently (esc)\n"
    "  Press enter to confirm or esc to cancel\n"
)

OPENCODE_PERMISSION_DIALOG = (
    '     $ echo "hi" > hello.txt\n'
    "     ▣  Build · Big Pickle\n"
    "  ┃\n"
    "  ┃  △ Permission required\n"
    "  ┃    # Shell command\n"
    "  ┃\n"
    '  ┃  $ echo "hi" > hello.txt\n'
    "  ┃\n"
    "  ┃   Allow once   Allow always   Reject          ctrl+f fullscreen  ⇆ select  enter confirm\n"
    "  ┃\n"
)


class TestPermissionDialogs:
    def test_claude_permission_dialog_is_waiting(self):
        adapter = get_terminal_adapter("claude")
        assert adapter.detect_waiting_for_human(CLAUDE_PERMISSION_DIALOG)
        assert not adapter.detect_idle(CLAUDE_PERMISSION_DIALOG)
        assert adapter.approve_keys == ("Enter",)

    def test_codex_permission_dialog_is_waiting(self):
        adapter = get_terminal_adapter("codex")
        assert adapter.detect_waiting_for_human(CODEX_PERMISSION_DIALOG)
        assert not adapter.detect_idle(CODEX_PERMISSION_DIALOG)
        assert adapter.approve_keys == ("Enter",)

    def test_opencode_permission_dialog_is_waiting(self):
        # Before this fixture the OpenCode adapter had no prompt markers at
        # all — a blocked member looked idle and deliveries were pasted into
        # the dialog.
        adapter = get_terminal_adapter("opencode")
        assert adapter.detect_waiting_for_human(OPENCODE_PERMISSION_DIALOG)
        assert not adapter.detect_idle(OPENCODE_PERMISSION_DIALOG)
        assert not adapter.detect_waiting_for_human(OPENCODE_IDLE_AFTER_RESPONSE)
        assert adapter.approve_keys == ("Enter",)

    def test_shell_has_no_answer(self):
        assert get_terminal_adapter("shell").approve_keys == ()


# The same dialogs a moment after they were answered: the text is still in
# the last 15 lines, but the runtime is back at its input. A state reading
# may still call this "waiting"; answering must not.
CODEX_DIALOG_ANSWERED = (
    CODEX_PERMISSION_DIALOG + "• Ran printf 'hi\\n' > hello.txt\n"
    "  └ (no output)\n"
    "› Ask Codex to do anything\n"
    "  gpt-5.6-sol default · /tmp/perm/codex\n"
)

CLAUDE_DIALOG_ANSWERED_WITH_TYPED_TEXT = (
    CLAUDE_PERMISSION_DIALOG + "⏺ Created hello.txt\n❯ now delete it\n  ? for shortcuts\n"
)

OPENCODE_DIALOG_ANSWERED = (
    OPENCODE_PERMISSION_DIALOG + "     hi\n"
    "     ▣  Build · Big Pickle · 2.1s\n"
    "┃  Build · Big Pickle OpenCode Zen\n"
    "   /tmp/perm/opencode                 8.7K (4%)  ctrl+p commands  • OpenCode 1.18.25\n"
)


class TestActiveDialogGate:
    def test_live_dialogs_are_active(self):
        assert get_terminal_adapter("claude").detect_active_dialog(CLAUDE_PERMISSION_DIALOG)
        assert get_terminal_adapter("codex").detect_active_dialog(CODEX_PERMISSION_DIALOG)
        assert get_terminal_adapter("opencode").detect_active_dialog(OPENCODE_PERMISSION_DIALOG)

    def test_answered_dialogs_are_not_active(self):
        # Stale "Press enter to confirm" above an idle prompt: Enter here would
        # submit whatever is typed at that prompt.
        codex = get_terminal_adapter("codex")
        assert codex.detect_waiting_for_human(CODEX_DIALOG_ANSWERED)  # loose state reading
        assert not codex.detect_active_dialog(CODEX_DIALOG_ANSWERED)  # strict answer gate
        claude = get_terminal_adapter("claude")
        assert not claude.detect_active_dialog(CLAUDE_DIALOG_ANSWERED_WITH_TYPED_TEXT)
        opencode = get_terminal_adapter("opencode")
        assert not opencode.detect_active_dialog(OPENCODE_DIALOG_ANSWERED)

    def test_unverified_runtimes_have_no_answer(self):
        assert get_terminal_adapter("gemini").approve_keys == ()
        assert get_terminal_adapter("aider").approve_keys == ()


class TestRuntimeDetection:
    def test_prompt_wins_over_stale_claude_history(self):
        pane = (
            "Previous diagnostic output: Claude Code runtime mismatch\n"
            "More history about opus 4.6 and delivery retries\n\n"
            "\u203a Explain this codebase\n\n"
            "  gpt-5.4 xhigh \u00b7 88% left \u00b7 ~/ws/core/code/WF/agent-backbone"
        )
        assert detect_runtime_from_pane(pane) == TerminalRuntime.CODEX


class TestPendingInput:
    """Typed text vs. everything else that can sit after the prompt character."""

    def test_codex_queued_input_footer_is_ignored_for_prompt_detection(self):
        pane = (
            "\u203a Second live inbound delivery test.\n\n"
            "  tab to queue message                                        98% context left"
        )
        assert prompt_has_pending_input(pane) is True

    def test_codex_queued_message_banner_is_ignored_for_prompt_detection(self):
        pane = (
            "\u2022 Messages to be submitted after next tool call "
            "(press esc to interrupt and send immediately)\n"
            "  \u21b3 [via:backbone from:bell] delivery check only.\n\n"
            "\u203a Summarize recent commits\n\n"
            "  gpt-5.4 xhigh \u00b7 59% left \u00b7 ~/ws/core/code/WF/agent-backbone"
        )
        assert prompt_has_pending_input(pane) is True

    @pytest.mark.parametrize(
        "pane",
        [
            "\u276f [via:backbone from:ike] Can you check the status?",
            "\u276f [via:github issue:51] [task] agent-backbone: Add topic routing",
            "\u276f [via:telegram from:elias] What's the status?",
        ],
    )
    def test_stuck_envelope_is_not_user_input(self, pane):
        # A delivery that was pasted but never consumed is not the human typing.
        assert prompt_has_pending_input(pane) is False

    def test_real_user_input_still_detected(self):
        assert prompt_has_pending_input("\u276f hello") is True

    def test_prefix_guard_suffix_matched_output_not_pending(self):
        # Claude's prompt is ❯ with suffix $: a line ending in $ without the
        # prefix matched via the suffix only and is output, not typed input.
        claude = get_terminal_adapter(TerminalRuntime.CLAUDE)
        assert claude.prompt_has_pending_input("some output line $") is False
