"""Tests for the runtime registry and the paste/detect behaviour of each runtime."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from agent_backbone.config import RUNTIMES as RUNTIME_IDS
from agent_backbone.services.runtimes import (
    RUNTIMES,
    UNKNOWN,
    detect_runtime,
    get_runtime,
    send_message,
)


class TestRegistry:
    def test_ids_match_the_settings_vocabulary(self):
        assert tuple(RUNTIMES) == RUNTIME_IDS

    def test_aliases_and_unknown(self):
        assert get_runtime("claude-code") is RUNTIMES["claude"]
        assert get_runtime("Claude Code") is RUNTIMES["claude"]
        assert get_runtime("gemini-cli") is RUNTIMES["gemini"]
        assert get_runtime(None) is UNKNOWN
        assert get_runtime("cursor") is UNKNOWN
        assert get_runtime(RUNTIMES["codex"]) is RUNTIMES["codex"]

    def test_brief_modes(self):
        assert {r.id: r.brief_mode for r in RUNTIMES.values()} == {
            "claude": "system_prompt",
            "codex": "initial_prompt",
            "gemini": "initial_prompt",
            "opencode": "initial_prompt",
            "aider": "message",
            "shell": "none",
        }

    def test_unknown_reads_like_a_shell_and_is_not_launchable(self):
        assert UNKNOWN.detect_idle("user@host $") is True
        assert "unknown" not in RUNTIMES
        assert UNKNOWN.binary is None


class TestSendMessage:
    async def test_send_success(self):
        mock_runtime = AsyncMock()
        mock_runtime.deliver_message = AsyncMock(return_value=True)
        with (
            patch(
                "agent_backbone.services.runtimes.capture_pane",
                new_callable=AsyncMock,
                return_value="\u203a ",
            ),
            patch(
                "agent_backbone.services.runtimes.resolve_runtime",
                new_callable=AsyncMock,
                return_value=mock_runtime,
            ) as mock_resolve,
        ):
            assert await send_message("ike", "hello") is True
        mock_resolve.assert_awaited_once_with(
            "ike",
            hint=None,
            pane_content="\u203a ",
        )
        mock_runtime.deliver_message.assert_awaited_once_with("ike", "hello")

    async def test_send_session_offline(self):
        mock_runtime = AsyncMock()
        mock_runtime.deliver_message = AsyncMock(return_value=False)
        with (
            patch(
                "agent_backbone.services.runtimes.capture_pane",
                new_callable=AsyncMock,
                return_value="",
            ),
            patch(
                "agent_backbone.services.runtimes.resolve_runtime",
                new_callable=AsyncMock,
                return_value=mock_runtime,
            ),
        ):
            assert await send_message("offline", "hello") is False

    async def test_passes_runtime_hint_to_adapter_resolution(self):
        mock_runtime = AsyncMock()
        mock_runtime.deliver_message = AsyncMock(return_value=True)
        with (
            patch(
                "agent_backbone.services.runtimes.capture_pane",
                new_callable=AsyncMock,
                return_value="",
            ),
            patch(
                "agent_backbone.services.runtimes.resolve_runtime",
                new_callable=AsyncMock,
                return_value=mock_runtime,
            ) as mock_resolve,
        ):
            assert await send_message("ike", "hello", runtime_hint="codex") is True
        mock_resolve.assert_awaited_once_with(
            "ike",
            hint="codex",
            pane_content="",
        )


class TestRuntimePaste:
    async def test_claude_adapter_submits_with_enter(self):
        runtime = RUNTIMES["claude"]
        with (
            patch(
                "agent_backbone.services.runtimes.base.paste_message",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_write,
            patch(
                "agent_backbone.services.runtimes.base.press_submit",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_submit,
            patch(
                "agent_backbone.services.runtimes.base.capture_pane",
                new_callable=AsyncMock,
                return_value="\u276f ",
            ),
            patch(
                "agent_backbone.services.runtimes.base.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            assert await runtime.deliver_message("ike", "hello") is True
        mock_write.assert_awaited_once_with("ike", "hello")
        mock_submit.assert_awaited_once_with("ike")

    async def test_codex_adapter_submits_and_retries_buffered_input(self):
        runtime = RUNTIMES["codex"]
        with (
            patch(
                "agent_backbone.services.runtimes.base.paste_message",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.runtimes.base.press_submit",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_submit,
            patch(
                "agent_backbone.services.runtimes.base.capture_pane",
                new_callable=AsyncMock,
                side_effect=["\u203a follow up", "\u203a "],
            ),
            patch(
                "agent_backbone.services.runtimes.base.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            assert await runtime.deliver_message("codex-repo", "hello") is True
        assert mock_submit.await_count == 2

    async def test_codex_adapter_interrupts_queued_delivery(self):
        runtime = RUNTIMES["codex"]
        with (
            patch(
                "agent_backbone.services.runtimes.base.paste_message",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.runtimes.base.press_submit",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_submit,
            patch(
                "agent_backbone.services.runtimes.base.press_escape",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_escape,
            patch(
                "agent_backbone.services.runtimes.base.capture_pane",
                new_callable=AsyncMock,
                side_effect=[
                    "\u2022 Messages to be submitted after next tool call\n\u203a hello",
                    "\u203a ",
                ],
            ),
            patch(
                "agent_backbone.services.runtimes.base.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            assert await runtime.deliver_message("codex-repo", "hello") is True
        mock_escape.assert_awaited_once_with("codex-repo")
        assert mock_submit.await_count == 2

    async def test_gemini_adapter_submits_with_enter(self):
        runtime = RUNTIMES["gemini"]
        with (
            patch(
                "agent_backbone.services.runtimes.base.paste_message",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.runtimes.base.press_submit",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_submit,
            patch(
                "agent_backbone.services.runtimes.base.capture_pane",
                new_callable=AsyncMock,
                return_value="> ",
            ),
            patch(
                "agent_backbone.services.runtimes.base.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            assert await runtime.deliver_message("gemini-repo", "hello") is True
        mock_submit.assert_awaited_once_with("gemini-repo")

    def test_runtime_detection_matches_live_prompt_samples(self):
        assert (
            detect_runtime(
                "\u203a Explain this codebase\n"
                "gpt-5.4 xhigh \u00b7 42% left \u00b7 ~/ws/core/code/WF/agent-backbone"
            ).id
            == "codex"
        )
        assert (
            detect_runtime(
                ">   Press 'Esc' for NORMAL mode.\n[INSERT] /model Auto (Gemini 3)\n? for shortcuts"
            ).id
            == "gemini"
        )
        assert (
            detect_runtime(
                "OpenCode\nAsk anything...\nctrl+t variants  tab agents  ctrl+p commands"
            ).id
            == "opencode"
        )


class TestLaunchEnvironment:
    def test_includes_agent_runtime_and_state_dir(self):
        from agent_backbone.services.agents.launch import launch_environment

        env = launch_environment("reviewer", "claude", "/data/state", {"FOO": "1"})
        assert env == {
            "BACKBONE_RUNTIME": "claude",
            "BACKBONE_AGENT": "reviewer",
            "BACKBONE_STATE_DIR": "/data/state",
            "FOO": "1",
        }


class TestClaudeCodeUi:
    """Regression fixtures captured from Claude Code (Aug 2026 UI)."""

    _BUSY = (
        "\u276f [via:backbone from:elias] Count slowly from 1 to 5.\n"
        "\u2733 Brewing\u2026 (esc to interrupt \u00b7 2s)\n"
        "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        "\u276f \n"
        "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        "  \u23f5\u23f5 auto mode on (shift+tab to cycle) \u00b7 \u2190 for agents\n"
    )
    _IDLE = (
        "\u23fa pong\n"
        "\u2733 Churned for 2s \u00b7 done 3:00 PM\n"
        "                                    \u25cf high \u00b7 /effort\n"
        "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        "\u276f \n"
        "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        "  \u23f5\u23f5 auto mode on (shift+tab to cycle) \u00b7 \u2190 for agents\n"
    )

    def test_detects_runtime_from_new_status_line(self):
        assert detect_runtime(self._IDLE).id == "claude"

    def test_idle_prompt_is_idle(self):
        runtime = RUNTIMES["claude"]
        assert runtime.detect_idle(self._IDLE) is True
        assert runtime.prompt_has_pending_input(self._IDLE) is False

    def test_spinner_means_busy_even_with_prompt_visible(self):
        runtime = RUNTIMES["claude"]
        assert runtime.detect_busy(self._BUSY) is True
        assert runtime.detect_idle(self._BUSY) is False

    def test_dim_suggestion_is_not_pending_input(self):
        runtime = RUNTIMES["claude"]
        pane = self._IDLE.replace("\u276f \n", "\u276f \x1b[2mCount from 5 down to 1\x1b[0m\n")
        assert runtime.prompt_has_pending_input(pane) is False

    def test_typed_text_is_pending_input(self):
        runtime = RUNTIMES["claude"]
        pane = self._IDLE.replace("\u276f \n", "\u276f fix the flaky test\n")
        assert runtime.prompt_has_pending_input(pane) is True

    async def test_input_buffered_while_busy_counts_as_queued(self):
        runtime = RUNTIMES["claude"]
        busy_with_input = self._BUSY.replace(
            "\u276f \n", "\u276f [via:backbone from:elias] second message\n"
        )
        with (
            patch(
                "agent_backbone.services.runtimes.base.paste_message",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.runtimes.base.press_submit",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.runtimes.base.capture_pane",
                new_callable=AsyncMock,
                return_value=busy_with_input.replace("[via:backbone from:elias] ", "typed "),
            ),
            patch("agent_backbone.services.runtimes.base.asyncio.sleep", new_callable=AsyncMock),
        ):
            assert await runtime.deliver_message("s", "second message") is True


class TestPaneLinePairing:
    def test_escape_only_line_does_not_shift_the_pairs(self):
        # tmux captures with -e can hold a line that is nothing but a cursor
        # sequence; it must not misalign raw and sanitized lines.
        pane = "\x1b[?25l\nsome output\n❯ fix the flaky test\n"
        runtime = RUNTIMES["claude"]
        assert runtime.detect_prompt(pane) == "❯ fix the flaky test"
        assert runtime.prompt_has_pending_input(pane) is True

    def test_non_sgr_escapes_do_not_count_as_typed_text(self):
        pane = "❯ \x1b[2mTry a suggestion\x1b[0m\x1b[K\n"
        assert RUNTIMES["claude"].prompt_has_pending_input(pane) is False
