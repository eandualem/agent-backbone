"""Async tmux operations.

Provides non-blocking tmux session checks and message delivery.
Uses asyncio.create_subprocess_exec instead of subprocess.run
to avoid blocking the event loop in Prefect flows.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


async def session_exists(session_name: str) -> bool:
    """Check if a tmux session exists."""
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "has-session",
        "-t",
        session_name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return proc.returncode == 0


async def send_message(session_name: str, message: str) -> bool:
    """Send a message to a tmux session.

    Returns True if delivery succeeded, False otherwise.
    Uses -l flag for literal text, then separate Enter key.
    """
    if not await session_exists(session_name):
        log.warning("tmux session '%s' not found — notification dropped", session_name)
        return False

    # Send message text literally (-l prevents key name interpretation)
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "send-keys",
        "-t",
        session_name,
        "-l",
        message,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("tmux send-keys failed for '%s': %s", session_name, stderr.decode())
        return False

    # Send Enter separately to submit
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "send-keys",
        "-t",
        session_name,
        "Enter",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("tmux send-keys Enter failed for '%s': %s", session_name, stderr.decode())
        return False

    log.info("Notification sent to tmux session '%s'", session_name)
    return True


async def send_keys(session_name: str, keys: str) -> bool:
    """Send raw tmux key sequence to a session (no -l flag, no Enter).

    Unlike send_message(), this sends keys that tmux interprets as key names
    or escape sequences. Used for special keys like Shift+Tab (Escape + [Z).

    Returns True if delivery succeeded, False otherwise.
    """
    if not await session_exists(session_name):
        log.warning("tmux session '%s' not found — key send dropped", session_name)
        return False

    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "send-keys",
        "-t",
        session_name,
        keys,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("tmux send-keys failed for '%s': %s", session_name, stderr.decode())
        return False

    log.info("Sent keys '%s' to tmux session '%s'", keys, session_name)
    return True


async def capture_pane(session_name: str, lines: int = 50) -> str:
    """Capture recent output from a tmux session's active pane.

    Returns the last N lines of visible pane content.
    Returns empty string if session doesn't exist or capture fails.
    """
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "capture-pane",
        "-t",
        session_name,
        "-p",  # output to stdout
        "-S",
        str(-lines),  # start N lines back
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return ""
    return stdout.decode()


async def start_session(session_name: str, command: str | None = None) -> bool:
    """Start a new detached tmux session.

    Returns True if the session was created, False if it already exists or failed.
    """
    if await session_exists(session_name):
        log.info("Session '%s' already exists", session_name)
        return True

    args = ["tmux", "new-session", "-d", "-s", session_name]
    if command:
        args.extend(["-c", command])

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("Failed to start session '%s': %s", session_name, stderr.decode())
        return False
    log.info("Started tmux session '%s'", session_name)
    return True


async def stop_session(session_name: str) -> bool:
    """Kill a tmux session.

    Returns True if session was killed, False if it didn't exist or failed.
    """
    if not await session_exists(session_name):
        log.info("Session '%s' does not exist", session_name)
        return True

    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "kill-session",
        "-t",
        session_name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("Failed to stop session '%s': %s", session_name, stderr.decode())
        return False
    log.info("Stopped tmux session '%s'", session_name)
    return True


async def list_sessions() -> list[str]:
    """List all active tmux session names."""
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "list-sessions",
        "-F",
        "#{session_name}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return []
    return [s.strip() for s in stdout.decode().splitlines() if s.strip()]


async def list_sessions_rich() -> list[dict]:
    """List sessions with metadata: name, windows, created, attached."""
    fmt = "#{session_name}\t#{session_windows}\t#{session_created}\t#{session_attached}"
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "list-sessions",
        "-F",
        fmt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return []
    results: list[dict] = []
    for line in stdout.decode().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        results.append(
            {
                "name": parts[0],
                "windows": int(parts[1]) if parts[1].isdigit() else 0,
                "created": int(parts[2]) if parts[2].isdigit() else 0,
                "attached": parts[3] == "1",
            }
        )
    return results
