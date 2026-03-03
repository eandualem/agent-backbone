"""Core tmux operations — session checks, message delivery, key sending, pane capture."""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

# Subprocess concurrency limiter — prevents storms from any caller
_MAX_CONCURRENT = 5
_semaphore = asyncio.Semaphore(_MAX_CONCURRENT)


async def _run_tmux(
    *args: str,
    capture_stdout: bool = False,
) -> tuple[int, bytes, bytes]:
    """Run a tmux subprocess with concurrency limiting.

    Returns (returncode, stdout, stderr). stdout is only captured
    when capture_stdout=True; otherwise it's empty bytes.
    """
    async with _semaphore:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            *args,
            stdout=asyncio.subprocess.PIPE if capture_stdout else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(10.0):
                stdout, stderr = await proc.communicate()
        except TimeoutError:
            try:
                proc.kill()
            except OSError:
                pass
            log.error("tmux command timed out: %s", " ".join(args))
            return -1, b"", b"tmux command timed out"

        return proc.returncode, stdout or b"", stderr or b""


async def session_exists(session_name: str) -> bool:
    """Check if a tmux session exists."""
    rc, _, _ = await _run_tmux("has-session", "-t", session_name)
    return rc == 0


async def send_message(session_name: str, message: str) -> bool:
    """Send a message to a tmux session.

    Returns True if delivery succeeded, False otherwise.
    Uses -l flag for literal text, then separate Enter key.
    """
    if not await session_exists(session_name):
        log.warning("tmux session '%s' not found — notification dropped", session_name)
        return False

    # Send message text literally (-l prevents key name interpretation)
    rc, _, stderr = await _run_tmux("send-keys", "-t", session_name, "-l", message)
    if rc != 0:
        log.error("tmux send-keys failed for '%s': %s", session_name, stderr.decode())
        return False

    # Send Enter separately to submit
    rc, _, stderr = await _run_tmux("send-keys", "-t", session_name, "Enter")
    if rc != 0:
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

    rc, _, stderr = await _run_tmux("send-keys", "-t", session_name, keys)
    if rc != 0:
        log.error("tmux send-keys failed for '%s': %s", session_name, stderr.decode())
        return False

    log.info("Sent keys '%s' to tmux session '%s'", keys, session_name)
    return True


async def resize_window(session_name: str, cols: int, rows: int) -> bool:
    """Resize a tmux session's current window.

    Explicitly sets the window dimensions so the tmux pane reflows content
    to match. Required when a browser terminal connects — TIOCSWINSZ on the
    PTY alone doesn't reliably resize when other clients are attached.

    Returns True on success, False on failure.
    """
    rc, _, stderr = await _run_tmux(
        "resize-window",
        "-t",
        session_name,
        "-x",
        str(cols),
        "-y",
        str(rows),
    )
    if rc != 0:
        log.warning("resize-window failed for '%s': %s", session_name, stderr.decode())
        return False
    return True


async def get_window_size(session_name: str) -> tuple[int, int] | None:
    """Query current tmux window dimensions (cols, rows).

    Returns None if session doesn't exist or query fails.
    """
    rc, stdout, _ = await _run_tmux(
        "display-message",
        "-t",
        session_name,
        "-p",
        "#{window_width} #{window_height}",
        capture_stdout=True,
    )
    if rc != 0:
        return None
    parts = stdout.decode().strip().split()
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


async def capture_pane(session_name: str, lines: int = 50) -> str:
    """Capture recent output from a tmux session's active pane.

    Returns the last N lines of visible pane content with ANSI escape
    sequences preserved (colors, bold, underline, cursor positioning).
    Returns empty string if session doesn't exist or capture fails.
    """
    rc, stdout, _ = await _run_tmux(
        "capture-pane",
        "-t",
        session_name,
        "-p",  # output to stdout
        "-e",  # include escape sequences (colors, formatting)
        "-S",
        str(-lines),  # start N lines back
        capture_stdout=True,
    )
    if rc != 0:
        return ""
    return stdout.decode()
