"""Tests for src/tmux.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.tmux import list_sessions, send_keys, send_message, session_exists


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
