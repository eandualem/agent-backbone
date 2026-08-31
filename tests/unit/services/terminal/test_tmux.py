"""Tests for agent_backbone/services/terminal."""

from __future__ import annotations

import signal
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.services.terminal import (
    TerminalRuntime,
    close_pane,
    close_window,
    create_layout,
    create_window,
    detect_runtime_from_pane,
    get_terminal_adapter,
    graceful_close,
    list_panes,
    list_sessions,
    list_windows,
    query_format_vars,
    rename_window,
    resize_pane,
    select_window,
    send_keys,
    send_message,
    session_exists,
    set_layout,
    split_pane,
    start_pipe_pane,
    start_session,
    stop_pipe_pane,
    swap_panes,
)


@pytest.fixture
def mock_subprocess():
    """Mock asyncio.create_subprocess_exec across all tmux private modules."""
    mock = AsyncMock()
    with (
        patch("agent_backbone.services.terminal._core.asyncio.create_subprocess_exec", mock),
        patch("agent_backbone.services.terminal._sessions.asyncio.create_subprocess_exec", mock),
        patch("agent_backbone.services.terminal._panes.asyncio.create_subprocess_exec", mock),
        patch("agent_backbone.services.terminal._windows.asyncio.create_subprocess_exec", mock),
        patch("agent_backbone.services.terminal._streaming.asyncio.create_subprocess_exec", mock),
    ):
        yield mock


class TestSessionExists:
    async def test_session_exists_true(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        assert await session_exists("ike") is True
        mock_subprocess.assert_called_once_with(
            "tmux",
            "has-session",
            "-t",
            "ike",
            stdout=-3,  # DEVNULL
            stderr=-1,  # PIPE
        )

    async def test_session_exists_false(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"no session"))
        mock_subprocess.return_value = proc

        assert await session_exists("nonexistent") is False


class TestSendMessage:
    async def test_send_success(self):
        mock_adapter = AsyncMock()
        mock_adapter.deliver_message = AsyncMock(return_value=True)
        with (
            patch(
                "agent_backbone.services.terminal._core.capture_pane",
                new_callable=AsyncMock,
                return_value="\u203a ",
            ),
            patch(
                "agent_backbone.services.terminal._adapters.get_terminal_adapter_for_session",
                new_callable=AsyncMock,
                return_value=mock_adapter,
            ) as mock_get_adapter,
        ):
            assert await send_message("ike", "hello") is True
        mock_get_adapter.assert_awaited_once_with(
            "ike",
            runtime_hint=None,
            pane_content="\u203a ",
        )
        mock_adapter.deliver_message.assert_awaited_once_with("ike", "hello")

    async def test_send_session_offline(self):
        mock_adapter = AsyncMock()
        mock_adapter.deliver_message = AsyncMock(return_value=False)
        with (
            patch(
                "agent_backbone.services.terminal._core.capture_pane",
                new_callable=AsyncMock,
                return_value="",
            ),
            patch(
                "agent_backbone.services.terminal._adapters.get_terminal_adapter_for_session",
                new_callable=AsyncMock,
                return_value=mock_adapter,
            ),
        ):
            assert await send_message("offline", "hello") is False

    async def test_passes_runtime_hint_to_adapter_resolution(self):
        mock_adapter = AsyncMock()
        mock_adapter.deliver_message = AsyncMock(return_value=True)
        with (
            patch(
                "agent_backbone.services.terminal._core.capture_pane",
                new_callable=AsyncMock,
                return_value="",
            ),
            patch(
                "agent_backbone.services.terminal._adapters.get_terminal_adapter_for_session",
                new_callable=AsyncMock,
                return_value=mock_adapter,
            ) as mock_get_adapter,
        ):
            assert await send_message("ike", "hello", runtime_hint="codex") is True
        mock_get_adapter.assert_awaited_once_with(
            "ike",
            runtime_hint="codex",
            pane_content="",
        )


class TestTerminalAdapters:
    async def test_claude_adapter_submits_with_enter(self):
        adapter = get_terminal_adapter(TerminalRuntime.CLAUDE)
        with (
            patch(
                "agent_backbone.services.terminal._adapters._write_message_buffer",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_write,
            patch(
                "agent_backbone.services.terminal._adapters._send_submit_key",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_submit,
            patch(
                "agent_backbone.services.terminal._adapters.capture_pane",
                new_callable=AsyncMock,
                return_value="\u276f ",
            ),
            patch(
                "agent_backbone.services.terminal._adapters.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            assert await adapter.deliver_message("ike", "hello") is True
        mock_write.assert_awaited_once_with("ike", "hello")
        mock_submit.assert_awaited_once_with("ike")

    async def test_codex_adapter_submits_and_retries_buffered_input(self):
        adapter = get_terminal_adapter(TerminalRuntime.CODEX)
        with (
            patch(
                "agent_backbone.services.terminal._adapters._write_message_buffer",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.terminal._adapters._send_submit_key",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_submit,
            patch(
                "agent_backbone.services.terminal._adapters.capture_pane",
                new_callable=AsyncMock,
                side_effect=["\u203a follow up", "\u203a "],
            ),
            patch(
                "agent_backbone.services.terminal._adapters.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            assert await adapter.deliver_message("codex-repo", "hello") is True
        assert mock_submit.await_count == 2

    async def test_codex_adapter_interrupts_queued_delivery(self):
        adapter = get_terminal_adapter(TerminalRuntime.CODEX)
        with (
            patch(
                "agent_backbone.services.terminal._adapters._write_message_buffer",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.terminal._adapters._send_submit_key",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_submit,
            patch(
                "agent_backbone.services.terminal._adapters._send_escape_key",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_escape,
            patch(
                "agent_backbone.services.terminal._adapters.capture_pane",
                new_callable=AsyncMock,
                side_effect=[
                    "\u2022 Messages to be submitted after next tool call\n\u203a hello",
                    "\u203a ",
                ],
            ),
            patch(
                "agent_backbone.services.terminal._adapters.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            assert await adapter.deliver_message("codex-repo", "hello") is True
        mock_escape.assert_awaited_once_with("codex-repo")
        assert mock_submit.await_count == 2

    async def test_gemini_adapter_submits_with_enter(self):
        adapter = get_terminal_adapter(TerminalRuntime.GEMINI)
        with (
            patch(
                "agent_backbone.services.terminal._adapters._write_message_buffer",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.terminal._adapters._send_submit_key",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_submit,
            patch(
                "agent_backbone.services.terminal._adapters.capture_pane",
                new_callable=AsyncMock,
                return_value="> ",
            ),
            patch(
                "agent_backbone.services.terminal._adapters.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            assert await adapter.deliver_message("gemini-repo", "hello") is True
        mock_submit.assert_awaited_once_with("gemini-repo")

    def test_runtime_detection_matches_live_prompt_samples(self):
        assert (
            detect_runtime_from_pane(
                "\u203a Explain this codebase\n"
                "gpt-5.4 xhigh \u00b7 42% left \u00b7 ~/ws/core/code/WF/agent-backbone"
            )
            == TerminalRuntime.CODEX
        )
        assert (
            detect_runtime_from_pane(
                ">   Press 'Esc' for NORMAL mode.\n[INSERT] /model Auto (Gemini 3)\n? for shortcuts"
            )
            == TerminalRuntime.GEMINI
        )
        assert (
            detect_runtime_from_pane(
                "OpenCode\nAsk anything...\nctrl+t variants  tab agents  ctrl+p commands"
            )
            == TerminalRuntime.OPENCODE
        )


class TestListSessions:
    async def test_list_sessions(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"feynman\nike\nleo\n", b""))
        mock_subprocess.return_value = proc

        result = await list_sessions()
        assert result == ["feynman", "ike", "leo"]

    async def test_list_sessions_no_server(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await list_sessions()
        assert result == []


class TestSendKeys:
    async def test_send_keys_success(self):
        with (
            patch(
                "agent_backbone.services.terminal._core.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.terminal._core.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
            ) as mock_exec,
        ):
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_exec.return_value = proc

            assert await send_keys("ike", "Escape") is True
            # Only called once (no Enter, no -l flag)
            assert mock_exec.call_count == 1
            call_args = mock_exec.call_args[0]
            assert "-l" not in call_args
            assert "Enter" not in call_args
            assert "Escape" in call_args

    async def test_send_keys_literal_uses_dash_l(self):
        with (
            patch(
                "agent_backbone.services.terminal._core.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.terminal._core.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
            ) as mock_exec,
        ):
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_exec.return_value = proc

            assert await send_keys("ike", "hello", literal=True) is True
            call_args = mock_exec.call_args[0]
            assert "-l" in call_args
            assert "hello" in call_args

    async def test_send_keys_session_offline(self):
        with patch(
            "agent_backbone.services.terminal._core.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            assert await send_keys("offline", "Escape") is False


class TestStartSession:
    async def test_start_with_working_dir_and_command(self, mock_subprocess):
        with patch(
            "agent_backbone.services.terminal._sessions.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.wait = AsyncMock()
            mock_subprocess.return_value = proc
            result = await start_session(
                "test",
                working_dir="/tmp/wd",
                command=["claude"],
            )
            assert result is True
            call_args = mock_subprocess.call_args_list[0][0]  # first call positional args
            assert "-c" in call_args
            assert "/tmp/wd" in call_args
            assert "claude" in call_args

    async def test_start_already_exists(self, mock_subprocess):
        with patch(
            "agent_backbone.services.terminal._sessions.session_exists",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await start_session("ike")
            assert result is True
            mock_subprocess.assert_not_called()

    async def test_start_without_working_dir(self, mock_subprocess):
        with patch(
            "agent_backbone.services.terminal._sessions.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.wait = AsyncMock()
            mock_subprocess.return_value = proc
            result = await start_session("test")
            assert result is True
            call_args = mock_subprocess.call_args_list[0][0]
            assert "-c" not in call_args

    async def test_start_with_multi_arg_command(self, mock_subprocess):
        """Multi-arg command list extends into subprocess args individually."""
        with patch(
            "agent_backbone.services.terminal._sessions.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.wait = AsyncMock()
            mock_subprocess.return_value = proc
            result = await start_session(
                "test",
                working_dir="/tmp/wd",
                command=["claude", "--model", "opus"],
            )
            assert result is True
            call_args = mock_subprocess.call_args_list[0][0]
            assert "claude" in call_args
            assert "--model" in call_args
            assert "opus" in call_args


class TestQueryFormatVars:
    async def test_returns_parsed_vars(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(
            return_value=(b"pane_in_mode=0\nclient_activity=1234567890\n", b"")
        )
        mock_subprocess.return_value = proc

        result = await query_format_vars("ike")
        assert result == {"pane_in_mode": "0", "client_activity": "1234567890"}

    async def test_returns_empty_on_failure(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await query_format_vars("nonexistent")
        assert result == {}

    async def test_custom_format_string(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"foo=bar\n", b""))
        mock_subprocess.return_value = proc

        result = await query_format_vars("ike", "foo=#{foo}")
        assert result == {"foo": "bar"}

    async def test_handles_empty_output(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await query_format_vars("ike")
        assert result == {}


class TestPipePane:
    async def test_start_pipe_pane_success(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await start_pipe_pane("ike", "/tmp/ike.log")
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "pipe-pane" in call_args
        assert "-t" in call_args
        assert "ike" in call_args
        assert "-o" in call_args
        assert "cat >> /tmp/ike.log" in call_args

    async def test_start_pipe_pane_failure(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_subprocess.return_value = proc

        result = await start_pipe_pane("ike", "/tmp/ike.log")
        assert result is False

    async def test_stop_pipe_pane_success(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await stop_pipe_pane("ike")
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "pipe-pane" in call_args
        assert "-t" in call_args
        assert "ike" in call_args
        assert "-o" not in call_args

    async def test_stop_pipe_pane_failure(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_subprocess.return_value = proc

        result = await stop_pipe_pane("ike")
        assert result is False


class TestListPanes:
    async def test_success(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"%1\t0\t120\t40\t1\n%2\t1\t120\t40\t0\n", b""))
        mock_subprocess.return_value = proc

        result = await list_panes("ike")
        assert len(result) == 2
        assert result[0]["pane_id"] == "%1"
        assert result[0]["pane_index"] == "0"
        assert result[0]["pane_width"] == "120"
        assert result[0]["pane_height"] == "40"
        assert result[0]["pane_active"] is True
        assert result[1]["pane_active"] is False

    async def test_empty_result(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await list_panes("ike")
        assert result == []

    async def test_failure(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await list_panes("nonexistent")
        assert result == []


class TestSplitPane:
    async def test_vertical(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await split_pane("ike", direction="vertical")
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "-v" in call_args
        assert "-h" not in call_args

    async def test_horizontal(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await split_pane("ike", direction="horizontal")
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "-h" in call_args
        assert "-v" not in call_args

    async def test_with_size(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await split_pane("ike", size="50%")
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "-l" in call_args
        assert "50%" in call_args

    async def test_failure(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_subprocess.return_value = proc

        result = await split_pane("ike")
        assert result is False


class TestResizePane:
    async def test_width(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await resize_pane("ike", "0", width=80)
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "-x" in call_args
        assert "80" in call_args

    async def test_height(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await resize_pane("ike", "0", height=24)
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "-y" in call_args
        assert "24" in call_args

    async def test_failure(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_subprocess.return_value = proc

        result = await resize_pane("ike", "0", width=80)
        assert result is False


class TestSwapPanes:
    async def test_success(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await swap_panes("ike", "0", "1")
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "-s" in call_args
        assert "ike:0" in call_args
        assert "-t" in call_args
        assert "ike:1" in call_args

    async def test_failure(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_subprocess.return_value = proc

        result = await swap_panes("ike", "0", "1")
        assert result is False


class TestClosePane:
    async def test_success(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await close_pane("ike", "1")
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "kill-pane" in call_args
        assert "ike:1" in call_args

    async def test_failure(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_subprocess.return_value = proc

        result = await close_pane("ike", "1")
        assert result is False


class TestSetLayout:
    async def test_tiled(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await set_layout("ike", "tiled")
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "select-layout" in call_args
        assert "tiled" in call_args

    async def test_even_horizontal(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await set_layout("ike", "even-horizontal")
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "even-horizontal" in call_args

    async def test_failure(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_subprocess.return_value = proc

        result = await set_layout("ike", "tiled")
        assert result is False


class TestCreateLayout:
    async def test_correct_split_count(self):
        """3 panes = 2 splits."""
        with (
            patch(
                "agent_backbone.services.terminal._windows.split_pane",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_split,
            patch(
                "agent_backbone.services.terminal._windows.set_layout",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.terminal._windows.list_panes",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "agent_backbone.services.terminal._windows.send_message",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            result = await create_layout("ike", {"panes": 3, "layout": "tiled"})
            assert result is True
            assert mock_split.call_count == 2

    async def test_layout_applied(self):
        with (
            patch(
                "agent_backbone.services.terminal._windows.split_pane",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.terminal._windows.set_layout",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_layout,
            patch(
                "agent_backbone.services.terminal._windows.list_panes",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "agent_backbone.services.terminal._windows.send_message",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            result = await create_layout("ike", {"panes": 2, "layout": "even-horizontal"})
            assert result is True
            mock_layout.assert_called_once_with("ike", "even-horizontal")

    async def test_split_failure_aborts(self):
        with (
            patch(
                "agent_backbone.services.terminal._windows.split_pane",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "agent_backbone.services.terminal._windows.set_layout",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_layout,
        ):
            result = await create_layout("ike", {"panes": 3, "layout": "tiled"})
            assert result is False
            mock_layout.assert_not_called()


class TestCreateWindow:
    async def test_minimal(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await create_window("ike")
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "new-window" in call_args
        assert "-n" not in call_args

    async def test_with_name(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await create_window("ike", name="logs")
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "-n" in call_args
        assert "logs" in call_args

    async def test_failure(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_subprocess.return_value = proc

        result = await create_window("ike")
        assert result is False


class TestCloseWindow:
    async def test_success(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await close_window("ike", "1")
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "kill-window" in call_args
        assert "ike:1" in call_args

    async def test_failure(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_subprocess.return_value = proc

        result = await close_window("ike", "1")
        assert result is False


class TestRenameWindow:
    async def test_success(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await rename_window("ike", "0", "new-name")
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "rename-window" in call_args
        assert "ike:0" in call_args
        assert "new-name" in call_args

    async def test_failure(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_subprocess.return_value = proc

        result = await rename_window("ike", "0", "new-name")
        assert result is False


class TestListWindows:
    async def test_success(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"@0\t0\tbash\t1\t1\n@1\t1\tlogs\t0\t2\n", b""))
        mock_subprocess.return_value = proc

        result = await list_windows("ike")
        assert len(result) == 2
        assert result[0]["window_id"] == "@0"
        assert result[0]["window_index"] == "0"
        assert result[0]["window_name"] == "bash"
        assert result[0]["window_active"] is True
        assert result[0]["window_panes"] == 1
        assert result[1]["window_active"] is False
        assert result[1]["window_panes"] == 2

    async def test_empty(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await list_windows("ike")
        assert result == []

    async def test_failure(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await list_windows("nonexistent")
        assert result == []


class TestSelectWindow:
    async def test_success(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await select_window("ike", "1")
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "select-window" in call_args
        assert "ike:1" in call_args

    async def test_failure(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_subprocess.return_value = proc

        result = await select_window("ike", "1")
        assert result is False


class TestStartSessionEnvironment:
    async def test_initial_command_receives_env_vars(self, mock_subprocess):
        """The initial tmux command inherits env vars when a command is supplied."""
        with patch(
            "agent_backbone.services.terminal._sessions.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.wait = AsyncMock()
            mock_subprocess.return_value = proc

            result = await start_session(
                "test",
                command=["uv", "run", "prefect", "worker", "start"],
                environment={"PREFECT_API_URL": "http://127.0.0.1:4200/api"},
            )

            assert result is True
            new_session_call = mock_subprocess.call_args_list[0][0]
            assert "new-session" in new_session_call
            assert "env" in new_session_call
            assert "PREFECT_API_URL=http://127.0.0.1:4200/api" in new_session_call
            assert "prefect" in new_session_call

    async def test_env_vars_set(self, mock_subprocess):
        """Environment variables are set via tmux set-environment."""
        with patch(
            "agent_backbone.services.terminal._sessions.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.wait = AsyncMock()
            mock_subprocess.return_value = proc

            result = await start_session(
                "test",
                environment={"MY_VAR": "hello", "OTHER": "world"},
            )
            assert result is True
            # First call: new-session, then 2 set-environment calls
            set_env_calls = [c for c in mock_subprocess.call_args_list if "set-environment" in c[0]]
            assert len(set_env_calls) == 2

    async def test_no_env_no_calls(self, mock_subprocess):
        """No environment param means no set-environment calls."""
        with patch(
            "agent_backbone.services.terminal._sessions.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.wait = AsyncMock()
            mock_subprocess.return_value = proc

            result = await start_session("test")
            assert result is True
            set_env_calls = [c for c in mock_subprocess.call_args_list if "set-environment" in c[0]]
            assert len(set_env_calls) == 0

    async def test_partial_failure_logs_warning(self, mock_subprocess):
        """If one env var fails, session still returns True."""
        with patch(
            "agent_backbone.services.terminal._sessions.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            # First call (new-session) succeeds, subsequent calls alternate
            success_proc = AsyncMock()
            success_proc.returncode = 0
            success_proc.communicate = AsyncMock(return_value=(b"", b""))
            success_proc.wait = AsyncMock()

            fail_proc = AsyncMock()
            fail_proc.returncode = 1
            fail_proc.communicate = AsyncMock(return_value=(b"", b"env error"))
            fail_proc.wait = AsyncMock()

            mock_subprocess.side_effect = [success_proc, fail_proc]

            result = await start_session(
                "test",
                environment={"BAD_VAR": "value"},
            )
            # Session creation succeeded, env var failure is non-fatal
            assert result is True


class TestGracefulClose:
    async def test_graceful_exit(self):
        """Process exits gracefully after SIGTERM."""
        with (
            patch(
                "agent_backbone.services.terminal._sessions.query_format_vars",
                new_callable=AsyncMock,
            ) as mock_qfv,
            patch(
                "agent_backbone.services.terminal._sessions.session_exists",
                new_callable=AsyncMock,
            ) as mock_exists,
            patch(
                "agent_backbone.services.terminal._sessions.stop_session",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_stop,
            patch("agent_backbone.services.terminal._sessions.os.kill") as mock_kill,
            patch(
                "agent_backbone.services.terminal._sessions.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            # First call: get pane_pid. Second call: pane_dead=1
            mock_qfv.side_effect = [
                {"pane_pid": "12345"},
                {"pane_dead": "1"},
            ]
            mock_exists.return_value = True

            result = await graceful_close("ike", timeout=5.0)
            assert result is True
            mock_kill.assert_called_once_with(12345, signal.SIGTERM)
            mock_stop.assert_called_once_with("ike")

    async def test_timeout_fallback(self):
        """Process doesn't exit within timeout, falls back to kill-session."""
        with (
            patch(
                "agent_backbone.services.terminal._sessions.query_format_vars",
                new_callable=AsyncMock,
            ) as mock_qfv,
            patch(
                "agent_backbone.services.terminal._sessions.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.terminal._sessions.stop_session",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_stop,
            patch("agent_backbone.services.terminal._sessions.os.kill"),
            patch(
                "agent_backbone.services.terminal._sessions.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            # pane_pid OK, then pane_dead always 0
            mock_qfv.side_effect = [
                {"pane_pid": "12345"},
                {"pane_dead": "0"},
                {"pane_dead": "0"},
            ]

            result = await graceful_close("ike", timeout=0.5)
            assert result is True
            # stop_session is the fallback
            mock_stop.assert_called()

    async def test_process_already_gone(self):
        """ProcessLookupError on kill means session cleanup."""
        with (
            patch(
                "agent_backbone.services.terminal._sessions.query_format_vars",
                new_callable=AsyncMock,
            ) as mock_qfv,
            patch(
                "agent_backbone.services.terminal._sessions.session_exists",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "agent_backbone.services.terminal._sessions.stop_session",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.terminal._sessions.os.kill",
                side_effect=ProcessLookupError,
            ),
        ):
            mock_qfv.return_value = {"pane_pid": "12345"}

            result = await graceful_close("ike")
            # Session doesn't exist either, so True
            assert result is True

    async def test_no_pid_fallback(self):
        """No pane_pid available means falls back to stop_session."""
        with (
            patch(
                "agent_backbone.services.terminal._sessions.query_format_vars",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "agent_backbone.services.terminal._sessions.stop_session",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_stop,
        ):
            result = await graceful_close("ike")
            assert result is True
            mock_stop.assert_called_once_with("ike")
