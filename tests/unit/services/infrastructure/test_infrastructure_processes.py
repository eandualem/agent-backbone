"""Tests for infrastructure process management."""

from __future__ import annotations

import signal
from unittest.mock import AsyncMock, patch

import pytest


class TestPidForPort:
    @pytest.mark.asyncio
    async def test_returns_pid_when_port_occupied(self):
        """pid_for_port returns PID when lsof finds a listener."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"12345\n", b"")
        mock_proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            from agent_backbone.services.infrastructure._processes import pid_for_port

            result = await pid_for_port(7120)
        assert result == 12345

    @pytest.mark.asyncio
    async def test_returns_none_when_port_free(self):
        """pid_for_port returns None when lsof finds nothing."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 1
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            from agent_backbone.services.infrastructure._processes import pid_for_port

            result = await pid_for_port(7120)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_output_not_digit(self):
        """pid_for_port returns None when lsof output is non-numeric."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"not-a-pid\n", b"")
        mock_proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            from agent_backbone.services.infrastructure._processes import pid_for_port

            result = await pid_for_port(7120)
        assert result is None

    @pytest.mark.asyncio
    async def test_takes_first_pid_from_multiline(self):
        """pid_for_port takes the first PID when lsof returns multiple lines."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"12345\n67890\n", b"")
        mock_proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            from agent_backbone.services.infrastructure._processes import pid_for_port

            result = await pid_for_port(7120)
        assert result == 12345


class TestCheckPortFree:
    @pytest.mark.asyncio
    async def test_true_when_free(self):
        """check_port_free returns True when no process listens."""
        with patch(
            "agent_backbone.services.infrastructure._processes.pid_for_port",
            new_callable=AsyncMock,
            return_value=None,
        ):
            from agent_backbone.services.infrastructure._processes import check_port_free

            assert await check_port_free(7120) is True

    @pytest.mark.asyncio
    async def test_false_when_occupied(self):
        """check_port_free returns False when a process is listening."""
        with patch(
            "agent_backbone.services.infrastructure._processes.pid_for_port",
            new_callable=AsyncMock,
            return_value=12345,
        ):
            from agent_backbone.services.infrastructure._processes import check_port_free

            assert await check_port_free(7120) is False


class TestKillPortProcess:
    @pytest.mark.asyncio
    async def test_returns_true_when_port_free(self):
        """kill_port_process returns True when port is already free."""
        with patch(
            "agent_backbone.services.infrastructure._processes.pid_for_port",
            new_callable=AsyncMock,
            return_value=None,
        ):
            from agent_backbone.services.infrastructure._processes import (
                kill_port_process,
            )

            assert await kill_port_process(7120) is True

    @pytest.mark.asyncio
    async def test_sigterm_kills_process(self):
        """Process exits after SIGTERM (alive check raises ProcessLookupError)."""
        kill_signals = []

        def mock_kill(pid, sig):
            kill_signals.append(sig)
            if sig == signal.SIGTERM:
                return
            if sig == 0:
                raise ProcessLookupError()

        with patch(
            "agent_backbone.services.infrastructure._processes.pid_for_port",
            new_callable=AsyncMock,
            return_value=999,
        ):
            with patch("os.kill", side_effect=mock_kill):
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    from agent_backbone.services.infrastructure._processes import (
                        kill_port_process,
                    )

                    result = await kill_port_process(7120, timeout=2.0)
        assert result is True
        assert signal.SIGTERM in kill_signals

    @pytest.mark.asyncio
    async def test_returns_true_when_process_already_gone(self):
        """kill_port_process returns True when SIGTERM raises ProcessLookupError."""
        with patch(
            "agent_backbone.services.infrastructure._processes.pid_for_port",
            new_callable=AsyncMock,
            return_value=999,
        ):
            with patch("os.kill", side_effect=ProcessLookupError()):
                from agent_backbone.services.infrastructure._processes import (
                    kill_port_process,
                )

                result = await kill_port_process(7120)
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_oserror(self):
        """kill_port_process returns False when SIGTERM fails with OSError."""
        with patch(
            "agent_backbone.services.infrastructure._processes.pid_for_port",
            new_callable=AsyncMock,
            return_value=999,
        ):
            with patch("os.kill", side_effect=OSError("Permission denied")):
                from agent_backbone.services.infrastructure._processes import (
                    kill_port_process,
                )

                result = await kill_port_process(7120)
        assert result is False


class TestWritePid:
    def test_creates_pid_file(self, tmp_path):
        """write_pid creates a file containing the PID."""
        with patch("agent_backbone.services.infrastructure._processes.PID_DIR", tmp_path):
            with patch("agent_backbone.services.infrastructure._processes._ensure_pid_dir"):
                from agent_backbone.services.infrastructure._processes import write_pid

                write_pid("gateway", 12345)
        assert (tmp_path / "gateway.pid").read_text() == "12345"


class TestReadPid:
    def test_returns_none_when_no_file(self, tmp_path):
        """read_pid returns None when PID file does not exist."""
        with patch("agent_backbone.services.infrastructure._processes.PID_DIR", tmp_path):
            from agent_backbone.services.infrastructure._processes import read_pid

            assert read_pid("gateway") is None

    def test_returns_pid_for_live_process(self, tmp_path):
        """read_pid returns PID when process is alive (os.kill(pid, 0) succeeds)."""
        (tmp_path / "gateway.pid").write_text("12345")
        with patch("agent_backbone.services.infrastructure._processes.PID_DIR", tmp_path):
            with patch("os.kill") as mock_kill:
                mock_kill.return_value = None  # Process is alive
                from agent_backbone.services.infrastructure._processes import read_pid

                assert read_pid("gateway") == 12345

    def test_cleans_stale_file(self, tmp_path):
        """read_pid removes PID file and returns None when process is dead."""
        pidfile = tmp_path / "gateway.pid"
        pidfile.write_text("99999")
        with patch("agent_backbone.services.infrastructure._processes.PID_DIR", tmp_path):
            with patch("os.kill", side_effect=ProcessLookupError()):
                from agent_backbone.services.infrastructure._processes import read_pid

                assert read_pid("gateway") is None
                assert not pidfile.exists()

    def test_returns_pid_on_permission_error(self, tmp_path):
        """read_pid returns PID when os.kill raises PermissionError (process exists)."""
        (tmp_path / "gateway.pid").write_text("12345")
        with patch("agent_backbone.services.infrastructure._processes.PID_DIR", tmp_path):
            with patch("os.kill", side_effect=PermissionError("Not permitted")):
                from agent_backbone.services.infrastructure._processes import read_pid

                assert read_pid("gateway") == 12345

    def test_cleans_invalid_pid_file(self, tmp_path):
        """read_pid removes PID file with non-integer content."""
        pidfile = tmp_path / "gateway.pid"
        pidfile.write_text("not-a-number")
        with patch("agent_backbone.services.infrastructure._processes.PID_DIR", tmp_path):
            from agent_backbone.services.infrastructure._processes import read_pid

            assert read_pid("gateway") is None
            assert not pidfile.exists()


class TestRemovePid:
    def test_removes_existing_file(self, tmp_path):
        """remove_pid deletes the PID file."""
        pidfile = tmp_path / "gateway.pid"
        pidfile.write_text("12345")
        with patch("agent_backbone.services.infrastructure._processes.PID_DIR", tmp_path):
            from agent_backbone.services.infrastructure._processes import remove_pid

            remove_pid("gateway")
        assert not pidfile.exists()

    def test_noop_when_no_file(self, tmp_path):
        """remove_pid is safe to call when file does not exist."""
        with patch("agent_backbone.services.infrastructure._processes.PID_DIR", tmp_path):
            from agent_backbone.services.infrastructure._processes import remove_pid

            remove_pid("nonexistent")  # Should not raise


class TestStopByPid:
    @pytest.mark.asyncio
    async def test_returns_true_when_no_pid(self, tmp_path):
        """stop_by_pid returns True when there is no PID file."""
        with patch("agent_backbone.services.infrastructure._processes.PID_DIR", tmp_path):
            from agent_backbone.services.infrastructure._processes import stop_by_pid

            assert await stop_by_pid("gateway") is True

    @pytest.mark.asyncio
    async def test_sigterm_then_cleanup(self, tmp_path):
        """stop_by_pid sends SIGTERM, verifies death, and removes PID file."""
        pidfile = tmp_path / "gateway.pid"
        pidfile.write_text("12345")

        kill_calls = []

        def mock_kill(pid, sig):
            kill_calls.append(sig)
            if sig == 0 and len(kill_calls) == 1:
                # First call: read_pid alive check -- process is alive
                return
            if sig == signal.SIGTERM:
                # Accept SIGTERM
                return
            if sig == 0:
                # Subsequent alive checks: process is dead
                raise ProcessLookupError()

        with patch("agent_backbone.services.infrastructure._processes.PID_DIR", tmp_path):
            with patch("os.kill", side_effect=mock_kill):
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    from agent_backbone.services.infrastructure._processes import (
                        stop_by_pid,
                    )

                    result = await stop_by_pid("gateway", timeout=2.0)
        assert result is True
        assert not pidfile.exists()
        assert signal.SIGTERM in kill_calls

    @pytest.mark.asyncio
    async def test_returns_true_when_process_gone_at_sigterm(self, tmp_path):
        """stop_by_pid returns True when SIGTERM raises ProcessLookupError."""
        pidfile = tmp_path / "gateway.pid"
        pidfile.write_text("12345")

        call_count = 0

        def mock_kill(pid, sig):
            nonlocal call_count
            call_count += 1
            if sig == 0 and call_count == 1:
                # First call: read_pid alive check -- process alive
                return
            if sig == signal.SIGTERM:
                # Process gone by the time we send SIGTERM
                raise ProcessLookupError()
            raise ProcessLookupError()

        with patch("agent_backbone.services.infrastructure._processes.PID_DIR", tmp_path):
            with patch("os.kill", side_effect=mock_kill):
                from agent_backbone.services.infrastructure._processes import (
                    stop_by_pid,
                )

                result = await stop_by_pid("gateway")
        assert result is True
        assert not pidfile.exists()

    @pytest.mark.asyncio
    async def test_returns_true_when_no_live_process(self, tmp_path):
        """stop_by_pid returns True when PID file exists but process is dead (stale file)."""
        pidfile = tmp_path / "gateway.pid"
        pidfile.write_text("12345")

        with patch("agent_backbone.services.infrastructure._processes.PID_DIR", tmp_path):
            with patch("os.kill", side_effect=ProcessLookupError()):
                from agent_backbone.services.infrastructure._processes import (
                    stop_by_pid,
                )

                # read_pid will return None (stale file), so stop_by_pid returns True
                result = await stop_by_pid("gateway")
        assert result is True


class TestRecordTmuxPid:
    @pytest.mark.asyncio
    async def test_records_pane_pid(self, tmp_path):
        """record_tmux_pid writes the pane PID to a file."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"54321\n", b"")
        mock_proc.returncode = 0

        with patch("agent_backbone.services.infrastructure._processes.PID_DIR", tmp_path):
            with patch("agent_backbone.services.infrastructure._processes._ensure_pid_dir"):
                with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                    with patch("asyncio.sleep", new_callable=AsyncMock):
                        from agent_backbone.services.infrastructure._processes import (
                            record_tmux_pid,
                        )

                        await record_tmux_pid("gateway", "gateway")
        assert (tmp_path / "gateway.pid").read_text() == "54321"

    @pytest.mark.asyncio
    async def test_noop_when_tmux_fails(self, tmp_path):
        """record_tmux_pid does nothing when tmux command fails."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"error")
        mock_proc.returncode = 1

        with patch("agent_backbone.services.infrastructure._processes.PID_DIR", tmp_path):
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    from agent_backbone.services.infrastructure._processes import (
                        record_tmux_pid,
                    )

                    await record_tmux_pid("gateway", "gateway")
        assert not (tmp_path / "gateway.pid").exists()
