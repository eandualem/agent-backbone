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
        "tmux", "has-session", "-t", session_name,
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
        "tmux", "send-keys", "-t", session_name, "-l", message,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("tmux send-keys failed for '%s': %s", session_name, stderr.decode())
        return False

    # Send Enter separately to submit
    proc = await asyncio.create_subprocess_exec(
        "tmux", "send-keys", "-t", session_name, "Enter",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("tmux send-keys Enter failed for '%s': %s", session_name, stderr.decode())
        return False

    log.info("Notification sent to tmux session '%s'", session_name)
    return True


async def list_sessions() -> list[str]:
    """List all active tmux session names."""
    proc = await asyncio.create_subprocess_exec(
        "tmux", "list-sessions", "-F", "#{session_name}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return []
    return [s.strip() for s in stdout.decode().splitlines() if s.strip()]
