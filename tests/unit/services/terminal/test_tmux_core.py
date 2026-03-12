"""Tests for src/agent_backbone/services/terminal/_core.py — subprocess semaphore."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from agent_backbone.services.terminal._core import (
    _run_tmux,
    capture_pane,
    get_window_size,
    resize_window,
    session_exists,
)

_CORE = "agent_backbone.services.terminal._core"


class TestRunTmux:
    async def test_returns_output_tuple(self):
        """_run_tmux returns (returncode, stdout, stderr) tuple."""
        with patch(f"{_CORE}.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"output", b""))
            proc.returncode = 0
            mock_exec.return_value = proc

            rc, stdout, stderr = await _run_tmux("has-session", "-t", "test", capture_stdout=True)

        assert rc == 0
        assert stdout == b"output"
        assert stderr == b""

    async def test_semaphore_limits_concurrency(self):
        """At most _MAX_CONCURRENT tmux subprocesses run simultaneously."""
        import agent_backbone.services.terminal._core as core_mod

        max_concurrent_seen = 0
        current_concurrent = 0
        lock = asyncio.Lock()

        original_semaphore = core_mod._semaphore
        # Use a semaphore with limit 5 (matching _MAX_CONCURRENT)
        core_mod._semaphore = asyncio.Semaphore(5)

        async def fake_exec(*args, **kwargs):
            nonlocal max_concurrent_seen, current_concurrent
            proc = AsyncMock()

            async def fake_communicate(input=None):
                nonlocal max_concurrent_seen, current_concurrent
                async with lock:
                    current_concurrent += 1
                    if current_concurrent > max_concurrent_seen:
                        max_concurrent_seen = current_concurrent
                await asyncio.sleep(0.05)
                async with lock:
                    current_concurrent -= 1
                return (b"", b"")

            proc.communicate = fake_communicate
            proc.returncode = 0
            return proc

        try:
            with patch(f"{_CORE}.asyncio.create_subprocess_exec", side_effect=fake_exec):
                # Launch 10 concurrent calls — semaphore should cap at 5
                coros = [_run_tmux("has-session", "-t", f"s{i}") for i in range(10)]
                await asyncio.gather(*coros)
        finally:
            core_mod._semaphore = original_semaphore

        assert max_concurrent_seen <= 5


class TestSessionExistsDelegatesToRunTmux:
    async def test_session_exists_uses_run_tmux(self):
        """session_exists delegates to _run_tmux."""
        with patch(f"{_CORE}._run_tmux", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, b"", b"")
            result = await session_exists("test-session")

        assert result is True
        mock_run.assert_called_once_with("has-session", "-t", "test-session")


class TestResizeWindowDelegatesToRunTmux:
    async def test_resize_window_success(self):
        """resize_window calls tmux resize-window with -x and -y."""
        with patch(f"{_CORE}._run_tmux", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, b"", b"")
            result = await resize_window("ike", 160, 35)

        assert result is True
        mock_run.assert_called_once_with("resize-window", "-t", "ike", "-x", "160", "-y", "35")

    async def test_resize_window_failure(self):
        """resize_window returns False on tmux error."""
        with patch(f"{_CORE}._run_tmux", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (1, b"", b"no session")
            result = await resize_window("ghost", 80, 24)

        assert result is False


class TestGetWindowSize:
    async def test_get_window_size_success(self):
        """Returns (cols, rows) tuple on successful tmux query."""
        with patch(f"{_CORE}._run_tmux", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, b"160 35\n", b"")
            result = await get_window_size("ike")

        assert result == (160, 35)
        mock_run.assert_called_once_with(
            "display-message",
            "-t",
            "ike",
            "-p",
            "#{window_width} #{window_height}",
            capture_stdout=True,
        )

    async def test_get_window_size_failure(self):
        """Returns None when tmux command fails."""
        with patch(f"{_CORE}._run_tmux", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (1, b"", b"error")
            result = await get_window_size("ghost")

        assert result is None

    async def test_get_window_size_bad_output(self):
        """Returns None when tmux output is unparseable."""
        with patch(f"{_CORE}._run_tmux", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, b"garbage", b"")
            result = await get_window_size("ike")

        assert result is None


class TestCapturePaneDelegatesToRunTmux:
    async def test_capture_pane_uses_run_tmux(self):
        """capture_pane delegates to _run_tmux with capture_stdout=True."""
        with patch(f"{_CORE}._run_tmux", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, b"hello world\n", b"")
            result = await capture_pane("test-session", lines=20)

        assert result == "hello world\n"
        mock_run.assert_called_once_with(
            "capture-pane",
            "-t",
            "test-session",
            "-p",
            "-e",
            "-S",
            "-20",
            capture_stdout=True,
        )
