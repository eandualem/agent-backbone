"""Tests for src/agent_backbone/services/terminal/_core.py — subprocess semaphore."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from agent_backbone.services.terminal._core import (
    _run_tmux,
    capture_pane,
    resize_window,
    session_exists,
    set_window_size_mode,
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

        # Start from a clean per-loop semaphore (limit _MAX_CONCURRENT == 5)
        core_mod._semaphores.clear()

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
            core_mod._semaphores.clear()

        assert max_concurrent_seen <= 5


class TestSessionExistsDelegatesToRunTmux:
    async def test_session_exists_uses_run_tmux(self):
        """session_exists delegates to _run_tmux."""
        with patch(f"{_CORE}._run_tmux", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, b"", b"")
            result = await session_exists("test-session")

        assert result is True
        mock_run.assert_called_once_with("has-session", "-t", "=test-session")


class TestResizeWindowDelegatesToRunTmux:
    async def test_resize_window_success(self):
        """resize_window calls tmux resize-window with -x and -y."""
        with patch(f"{_CORE}._run_tmux", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, b"", b"")
            result = await resize_window("ike", 160, 35)

        assert result is True
        mock_run.assert_called_once_with("resize-window", "-t", "=ike", "-x", "160", "-y", "35")

    async def test_resize_window_failure(self):
        """resize_window returns False on tmux error."""
        with patch(f"{_CORE}._run_tmux", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (1, b"", b"no session")
            result = await resize_window("ghost", 80, 24)

        assert result is False


class TestSetWindowSizeMode:
    async def test_sets_latest_mode(self):
        """Sets tmux window-size mode on the target session."""
        with patch(f"{_CORE}._run_tmux", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, b"", b"")
            result = await set_window_size_mode("ike", "latest")

        assert result is True
        mock_run.assert_called_once_with(
            "set-window-option",
            "-t",
            "=ike",
            "window-size",
            "latest",
        )

    async def test_returns_false_on_tmux_error(self):
        """tmux failures are reported as False."""
        with patch(f"{_CORE}._run_tmux", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (1, b"", b"bad target")
            result = await set_window_size_mode("ike", "latest")

        assert result is False

    async def test_rejects_unknown_mode(self):
        """Unsupported window-size policies fail fast."""
        try:
            await set_window_size_mode("ike", "sideways")
        except ValueError as exc:
            assert "Unsupported tmux window-size mode" in str(exc)
        else:
            raise AssertionError("Expected ValueError for unsupported mode")


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
            "=test-session",
            "-p",
            "-e",
            "-S",
            "-20",
            capture_stdout=True,
        )
