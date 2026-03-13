"""PID-file helpers for PTY orphan cleanup."""

from __future__ import annotations

import logging
import signal
from collections.abc import Callable
from pathlib import Path


def clear_orphaned_pids(
    pid_file: Path,
    *,
    kill_pid: Callable[[int, int], None],
    logger: logging.Logger,
) -> None:
    """Kill tracked orphaned PTY processes and clear the tracking file."""
    try:
        if not pid_file.exists():
            return
        content = pid_file.read_text().strip()
        if not content:
            return
        pids = [int(pid) for pid in content.split("\n") if pid.strip()]
        killed = 0
        for pid in pids:
            try:
                kill_pid(pid, signal.SIGTERM)
                killed += 1
                logger.info("Killed orphaned tmux attach-session (pid=%d)", pid)
            except OSError:
                pass
        if killed:
            logger.info("Cleaned up %d orphaned PTY process(es)", killed)
        pid_file.write_text("")
    except Exception:
        logger.warning("Failed to clean up orphaned PTY processes", exc_info=True)


def append_pid(pid_file: Path, pid: int, *, logger: logging.Logger) -> None:
    """Append a PID to the PTY tracking file."""
    try:
        with pid_file.open("a") as handle:
            handle.write(f"{pid}\n")
    except OSError:
        logger.debug("Failed to record PID %d", pid)


def remove_pid(pid_file: Path, pid: int, *, logger: logging.Logger) -> None:
    """Remove a PID from the PTY tracking file."""
    try:
        if not pid_file.exists():
            return
        lines = pid_file.read_text().strip().split("\n")
        remaining = [line for line in lines if line.strip() and line.strip() != str(pid)]
        pid_file.write_text("\n".join(remaining) + "\n" if remaining else "")
    except OSError:
        logger.debug("Failed to unrecord PID %d", pid)
