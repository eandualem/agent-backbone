"""Explicit server selection stays fixed across every terminal access path."""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_backbone.services.terminal import (
    PtyManager,
    PtySession,
    _server,
    attach_session,
    pin_tmux_server,
    tmux_server_socket,
)
from agent_backbone.services.terminal._core import _run_tmux
from agent_backbone.services.terminal._pty import _parse_pid_record, _process_identity, _record_pid


@pytest.fixture(autouse=True)
def fresh_process_selection(monkeypatch):
    monkeypatch.setattr(_server, "_socket", None)
    monkeypatch.setattr(_server, "_used", False)


def test_selection_is_one_time_and_read_does_not_select(tmp_path):
    assert tmux_server_socket() is None
    socket = tmp_path / "server.sock"
    pin_tmux_server(socket)
    assert tmux_server_socket() == str(socket.resolve())
    assert not socket.exists()
    pin_tmux_server(socket)
    with pytest.raises(ValueError, match="already fixed"):
        pin_tmux_server(tmp_path / "other.sock")


@pytest.mark.parametrize("path", ["relative.sock", "/tmp/../server.sock", "/tmp/bad\nsock"])
def test_invalid_selection_does_not_pin(path):
    with pytest.raises(ValueError, match="absolute normalized"):
        pin_tmux_server(Path(path))
    assert tmux_server_socket() is None


async def test_legacy_operation_prevents_later_pin(tmp_path):
    process = AsyncMock(returncode=0)
    process.communicate.return_value = (b"", b"")
    with patch("asyncio.create_subprocess_exec", return_value=process) as run:
        await _run_tmux("list-sessions")
    assert run.call_args.args == ("tmux", "list-sessions")
    with pytest.raises(ValueError, match="already fixed"):
        pin_tmux_server(tmp_path / "server.sock")


async def test_subprocess_uses_pinned_socket_despite_ambient_selection(tmp_path, monkeypatch):
    socket = tmp_path / "server.sock"
    pin_tmux_server(socket)
    monkeypatch.setenv("TMUX", "/other/server,123,0")
    monkeypatch.setenv("TMUX_TMPDIR", "/other")
    process = AsyncMock(returncode=0)
    process.communicate.return_value = (b"", b"")
    with patch("asyncio.create_subprocess_exec", return_value=process) as run:
        await _run_tmux("capture-pane", "-t", "=worker:")
    assert run.call_args.args == ("tmux", "-S", str(socket), "capture-pane", "-t", "=worker:")
    pin_tmux_server(socket)


@pytest.mark.parametrize("inside", [False, True])
def test_attach_uses_selected_server(tmp_path, monkeypatch, inside):
    socket = tmp_path / "server.sock"
    pin_tmux_server(socket)
    if inside:
        monkeypatch.setenv("TMUX", f"{socket},123,0")
    with patch("agent_backbone.services.terminal._attach.subprocess.call", return_value=0) as run:
        assert attach_session("worker") == 0
    run.assert_called_once_with(
        [
            "tmux",
            "-S",
            str(socket),
            "switch-client" if inside else "attach-session",
            "-t",
            "=worker:",
        ]
    )


def test_attach_cannot_switch_a_client_from_another_server(tmp_path, monkeypatch):
    pin_tmux_server(tmp_path / "server.sock")
    monkeypatch.setenv("TMUX", "/other/server,123,0")
    with patch("agent_backbone.services.terminal._attach.subprocess.call") as run:
        with pytest.raises(ValueError, match="another terminal"):
            attach_session("worker")
    run.assert_not_called()


async def test_pty_and_record_use_exact_selected_server(tmp_path):
    socket = tmp_path / "server.sock"
    pin_tmux_server(socket)
    command = ["tmux", "-S", str(socket), "attach-session", "-t", "=worker:"]
    record = {"pid": 12345, "birth": "Sat Sep 05 10:20:30 2026", "command": command}
    assert _parse_pid_record(json.dumps(record)) == record
    pid_file = tmp_path / "pty-pids.txt"
    with (
        patch("agent_backbone.services.terminal._pty.subprocess.Popen") as run,
        patch("agent_backbone.services.terminal._pty._process_identity", return_value=record),
        patch.object(PtySession, "_read_loop", new_callable=AsyncMock),
    ):
        run.return_value.pid = 12345
        session = PtySession("worker", pid_file)
        try:
            session.start()
            await session._reader_task
            assert run.call_args.args[0] == command
            assert json.loads(pid_file.read_text()) == record
        finally:
            await session.cleanup()
    record["command"][2] = str(tmp_path / "other.sock")
    with patch("agent_backbone.services.terminal._pty._process_identity", return_value=record):
        _record_pid(pid_file, 12345, "worker")
    assert pid_file.read_text() == ""


@pytest.mark.parametrize("other_server", [False, True])
def test_orphan_cleanup_stays_on_selected_server(tmp_path, other_server):
    socket = tmp_path / "selected.sock"
    pin_tmux_server(socket)
    command = [
        "tmux",
        "-S",
        str(tmp_path / "other.sock" if other_server else socket),
        "attach-session",
        "-t",
        "=worker:",
    ]
    record = {"pid": 12345, "birth": "Sat Sep 05 10:20:30 2026", "command": command}
    pid_file = tmp_path / "pty-pids.txt"
    pid_file.write_text(json.dumps(record) + "\n")
    with (
        patch("agent_backbone.services.terminal._pty._process_identity", return_value=record),
        patch("agent_backbone.services.terminal._pty.os.kill") as kill,
    ):
        PtyManager(pid_file)
    assert kill.call_count == (0 if other_server else 1)
    assert pid_file.read_text() == (json.dumps(record) + "\n" if other_server else "")


@pytest.mark.parametrize(
    "name", ["socket with spaces", "socket'quoted", 'socket"quoted', r"a\b", "café"]
)
def test_ps_pinned_path_is_not_shell_syntax(tmp_path, name):
    socket = tmp_path / name
    pin_tmux_server(socket)
    command = ["tmux", "-S", str(socket), "attach-session", "-t", "=worker:"]
    raw = "Sat Sep 05 10:20:30 2026 " + " ".join(command)
    with patch(
        "agent_backbone.services.terminal._pty.subprocess.run",
        return_value=MagicMock(returncode=0, stdout=raw),
    ) as run:
        assert _process_identity(12345) == {
            "pid": 12345,
            "birth": "Sat Sep 05 10:20:30 2026",
            "command": command,
        }
    assert run.call_args.kwargs["encoding"] == "utf-8"
    assert run.call_args.kwargs["env"]["LC_ALL"] == (
        "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"
    )
