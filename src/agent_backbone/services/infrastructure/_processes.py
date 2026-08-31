"""Port checks, PID file management, and process kill escalation."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

log = logging.getLogger(__name__)

_DEFAULT_DATA_DIR = os.environ.get("BACKBONE_DATA_DIR", "~/.local/share/agent-backbone")
PID_DIR = Path(_DEFAULT_DATA_DIR).expanduser() / "pids"


def set_pid_dir(path: Path) -> None:
    """Point PID files at a directory (called once from config at startup)."""
    global PID_DIR
    PID_DIR = Path(path)


def _ensure_pid_dir() -> None:
    """Create PID directory if it doesn't exist."""
    PID_DIR.mkdir(parents=True, exist_ok=True)


async def pid_for_port(port: int) -> int | None:
    """Return the PID listening on the given port, or None."""
    proc = await asyncio.create_subprocess_exec(
        "lsof",
        "-ti",
        f":{port}",
        "-sTCP:LISTEN",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0 or not stdout:
        return None
    first_line = stdout.decode().strip().split("\n")[0].strip()
    if first_line.isdigit():
        return int(first_line)
    return None


async def check_port_free(port: int) -> bool:
    """Return True if no process is listening on the port."""
    return await pid_for_port(port) is None


async def kill_port_process(port: int, timeout: float = 5.0) -> bool:
    """Kill the process listening on a port with SIGTERM -> SIGKILL escalation.

    Returns True if the port is free after this call.
    """
    pid = await pid_for_port(port)
    if pid is None:
        return True

    # SIGTERM first
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError as e:
        log.warning("Failed to SIGTERM pid %d on port %d: %s", pid, port, e)
        return False

    # Wait for process to exit
    elapsed = 0.0
    while elapsed < timeout:
        await asyncio.sleep(1.0)
        elapsed += 1.0
        try:
            os.kill(pid, 0)  # Check if alive
        except ProcessLookupError:
            return True

    # SIGKILL
    log.warning("Process %d didn't exit after %.0fs SIGTERM, sending SIGKILL", pid, timeout)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        pass

    await asyncio.sleep(1.0)
    try:
        os.kill(pid, 0)
        log.error("Process %d still alive after SIGKILL", pid)
        return False
    except ProcessLookupError:
        return True


def write_pid(service: str, pid: int) -> None:
    """Write a PID file for a service."""
    _ensure_pid_dir()
    (PID_DIR / f"{service}.pid").write_text(str(pid))


def read_pid(service: str) -> int | None:
    """Read PID from file. Returns PID if process is alive, cleans stale files."""
    pidfile = PID_DIR / f"{service}.pid"
    if not pidfile.exists():
        return None
    try:
        pid = int(pidfile.read_text().strip())
    except (ValueError, OSError):
        pidfile.unlink(missing_ok=True)
        return None

    try:
        os.kill(pid, 0)  # Check if alive
        return pid
    except ProcessLookupError:
        pidfile.unlink(missing_ok=True)
        return None
    except OSError:
        return pid  # Permission error — process exists


def remove_pid(service: str) -> None:
    """Remove a PID file."""
    (PID_DIR / f"{service}.pid").unlink(missing_ok=True)


async def stop_by_pid(service: str, timeout: float = 5.0) -> bool:
    """Stop a service by its PID file with SIGTERM -> SIGKILL escalation."""
    pid = read_pid(service)
    if pid is None:
        return True

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        remove_pid(service)
        return True
    except OSError:
        remove_pid(service)
        return True

    elapsed = 0.0
    while elapsed < timeout:
        await asyncio.sleep(1.0)
        elapsed += 1.0
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            remove_pid(service)
            return True

    log.warning("Process %d (%s) didn't exit after %.0fs, sending SIGKILL", pid, service, timeout)
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    await asyncio.sleep(1.0)
    remove_pid(service)
    return True


async def record_tmux_pid(service: str, session: str) -> None:
    """Record the PID of the process running inside a tmux pane."""
    await asyncio.sleep(0.5)  # Brief pause for process to spawn
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "list-panes",
        "-t",
        session,
        "-F",
        "#{pane_pid}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode == 0 and stdout:
        pid_str = stdout.decode().strip().split("\n")[0].strip()
        if pid_str.isdigit():
            write_pid(service, int(pid_str))
