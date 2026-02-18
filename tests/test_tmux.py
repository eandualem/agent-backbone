"""Tests for src/tmux.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.tmux import (
    list_sessions,
    query_format_vars,
    resolve_agent_dir,
    send_keys,
    send_message,
    session_exists,
    start_pipe_pane,
    start_session,
    stop_pipe_pane,
)


@pytest.fixture
def mock_subprocess():
    """Mock asyncio.create_subprocess_exec."""
    with patch("src.tmux.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock:
        yield mock


class TestSessionExists:
    async def test_session_exists_true(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.wait = AsyncMock()
        mock_subprocess.return_value = proc

        assert await session_exists("ike") is True
        mock_subprocess.assert_called_once_with(
            "tmux",
            "has-session",
            "-t",
            "ike",
            stdout=-3,  # DEVNULL
            stderr=-3,
        )

    async def test_session_exists_false(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.wait = AsyncMock()
        mock_subprocess.return_value = proc

        assert await session_exists("nonexistent") is False


class TestSendMessage:
    async def test_send_success(self):
        with (
            patch("src.tmux.session_exists", new_callable=AsyncMock, return_value=True),
            patch("src.tmux.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
        ):
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_exec.return_value = proc

            assert await send_message("ike", "hello") is True
            # Called twice: once for -l message, once for Enter
            assert mock_exec.call_count == 2

    async def test_send_session_offline(self):
        with patch("src.tmux.session_exists", new_callable=AsyncMock, return_value=False):
            assert await send_message("offline", "hello") is False

    async def test_send_keys_failure(self):
        with (
            patch("src.tmux.session_exists", new_callable=AsyncMock, return_value=True),
            patch("src.tmux.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
        ):
            proc = AsyncMock()
            proc.returncode = 1
            proc.communicate = AsyncMock(return_value=(b"", b"error"))
            mock_exec.return_value = proc

            assert await send_message("ike", "hello") is False


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
            patch("src.tmux.session_exists", new_callable=AsyncMock, return_value=True),
            patch("src.tmux.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
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

    async def test_send_keys_session_offline(self):
        with patch("src.tmux.session_exists", new_callable=AsyncMock, return_value=False):
            assert await send_keys("offline", "Escape") is False


class TestResolveAgentDir:
    def test_named_entity_feynman(self):
        result = resolve_agent_dir("feynman")
        assert result.endswith("orchestration")

    def test_named_entity_ike(self):
        result = resolve_agent_dir("ike")
        assert "ws/core/ike" in result

    def test_coding_repo_found(self, tmp_path):
        with patch("src.tmux._CODE_BASE_DIRS", [tmp_path]):
            (tmp_path / "my-repo").mkdir()
            result = resolve_agent_dir("my-repo")
            assert result == str(tmp_path / "my-repo")

    def test_unknown_returns_empty(self):
        result = resolve_agent_dir("nonexistent-xyz")
        assert result == ""


class TestStartSession:
    async def test_start_with_working_dir_and_command(self, mock_subprocess):
        with patch("src.tmux.session_exists", new_callable=AsyncMock, return_value=False):
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.wait = AsyncMock()
            mock_subprocess.return_value = proc
            result = await start_session(
                "test",
                working_dir="/tmp/wd",
                command="claude",
                apply_theme=False,
            )
            assert result is True
            call_args = mock_subprocess.call_args_list[0][0]  # first call positional args
            assert "-c" in call_args
            assert "/tmp/wd" in call_args
            assert "claude" in call_args

    async def test_start_already_exists(self, mock_subprocess):
        with patch("src.tmux.session_exists", new_callable=AsyncMock, return_value=True):
            result = await start_session("ike", apply_theme=False)
            assert result is True
            mock_subprocess.assert_not_called()

    async def test_start_without_working_dir(self, mock_subprocess):
        with patch("src.tmux.session_exists", new_callable=AsyncMock, return_value=False):
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.wait = AsyncMock()
            mock_subprocess.return_value = proc
            result = await start_session("test", apply_theme=False)
            assert result is True
            call_args = mock_subprocess.call_args_list[0][0]
            assert "-c" not in call_args


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
