"""Core tmux operations — session checks, message delivery, key sending, pane capture."""

from __future__ import annotations

import asyncio
import contextlib
import logging

log = logging.getLogger(__name__)

# Subprocess concurrency limiter — prevents storms from any caller
_MAX_CONCURRENT = 5
_semaphores: dict[int, asyncio.Semaphore] = {}


def _get_semaphore() -> asyncio.Semaphore:
    """Per-event-loop concurrency limiter for tmux subprocess calls.

    asyncio primitives bind to the loop that first uses them, so a module-level
    semaphore breaks when the process runs more than one loop (tests, reloads).
    """
    loop_id = id(asyncio.get_running_loop())
    sem = _semaphores.get(loop_id)
    if sem is None:
        _semaphores.clear()
        sem = _semaphores[loop_id] = asyncio.Semaphore(_MAX_CONCURRENT)
    return sem


async def _run_tmux(
    *args: str,
    capture_stdout: bool = False,
    stdin_data: bytes | None = None,
) -> tuple[int, bytes, bytes]:
    """Run a tmux subprocess with concurrency limiting.

    Returns (returncode, stdout, stderr). stdout is only captured
    when capture_stdout=True; otherwise it's empty bytes.
    When stdin_data is provided, it is piped to the process's stdin.
    """
    async with _get_semaphore():
        kwargs: dict = {
            "stdout": asyncio.subprocess.PIPE if capture_stdout else asyncio.subprocess.DEVNULL,
            "stderr": asyncio.subprocess.PIPE,
        }
        if stdin_data is not None:
            kwargs["stdin"] = asyncio.subprocess.PIPE
        proc = await asyncio.create_subprocess_exec("tmux", *args, **kwargs)
        try:
            async with asyncio.timeout(10.0):
                stdout, stderr = await proc.communicate(input=stdin_data)
        except TimeoutError:
            with contextlib.suppress(OSError):
                proc.kill()
            log.error("tmux command timed out: %s", " ".join(args))
            return -1, b"", b"tmux command timed out"

        return proc.returncode, stdout or b"", stderr or b""


async def session_exists(session_name: str) -> bool:
    """Check if a tmux session exists."""
    rc, _, _ = await _run_tmux("has-session", "-t", session_name)
    return rc == 0


async def _send_submit_key(session_name: str) -> bool:
    """Send Enter to submit the currently buffered prompt input."""
    rc, _, stderr = await _run_tmux("send-keys", "-t", session_name, "Enter")
    if rc != 0:
        log.error("tmux send-keys Enter failed for '%s': %s", session_name, stderr.decode())
        return False
    return True


async def _send_escape_key(session_name: str) -> bool:
    """Send Escape to interrupt active work and expose queued input."""
    rc, _, stderr = await _run_tmux("send-keys", "-t", session_name, "Escape")
    if rc != 0:
        log.error("tmux send-keys Escape failed for '%s': %s", session_name, stderr.decode())
        return False
    return True


async def _write_message_buffer(session_name: str, message: str) -> bool:
    """Paste a message into the tmux session input buffer."""
    if not await session_exists(session_name):
        log.warning("tmux session '%s' not found — notification dropped", session_name)
        return False

    rc, _, stderr = await _run_tmux("load-buffer", "-", stdin_data=message.encode())
    if rc != 0:
        log.error("tmux load-buffer failed for '%s': %s", session_name, stderr.decode())
        return False

    rc, _, stderr = await _run_tmux("paste-buffer", "-t", session_name, "-d")
    if rc != 0:
        log.error("tmux paste-buffer failed for '%s': %s", session_name, stderr.decode())
        return False
    return True


async def send_message(
    session_name: str,
    message: str,
    *,
    runtime_hint: str | None = None,
) -> bool:
    """Send a message to a tmux session via the runtime-specific adapter."""
    from agent_backbone.services.terminal._adapters import get_terminal_adapter_for_session

    pane_content = await capture_pane(session_name, lines=80)
    adapter = await get_terminal_adapter_for_session(
        session_name,
        runtime_hint=runtime_hint,
        pane_content=pane_content,
    )
    return await adapter.deliver_message(session_name, message)


async def send_keys(session_name: str, keys: str, *, literal: bool = False) -> bool:
    """Send raw tmux keys to a session.

    When ``literal`` is False, tmux interprets ``keys`` as key names or
    escape sequences (for example ``Enter`` or ``Escape``). When True, the
    characters are sent literally with ``send-keys -l``.
    """
    if not await session_exists(session_name):
        log.warning("tmux session '%s' not found — key send dropped", session_name)
        return False

    args = ["send-keys", "-t", session_name]
    if literal:
        args.append("-l")
    args.append(keys)
    rc, _, stderr = await _run_tmux(*args)
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


async def set_window_size_mode(session_name: str, mode: str) -> bool:
    """Set tmux's window-size policy for a session window."""
    if mode not in {"latest", "largest", "smallest", "manual"}:
        raise ValueError(f"Unsupported tmux window-size mode: {mode}")

    rc, _, stderr = await _run_tmux(
        "set-window-option",
        "-t",
        session_name,
        "window-size",
        mode,
    )
    if rc != 0:
        log.warning(
            "set-window-option window-size failed for '%s' (%s): %s",
            session_name,
            mode,
            stderr.decode(),
        )
        return False
    return True


async def active_pane_size(session_name: str) -> tuple[int, int] | None:
    """(cols, rows) of the session's active pane, or None if unavailable."""
    rc, stdout, _ = await _run_tmux(
        "display-message",
        "-t",
        session_name,
        "-p",
        "#{pane_width} #{pane_height}",
        capture_stdout=True,
    )
    if rc != 0:
        return None
    parts = stdout.decode().split()
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    return int(parts[0]), int(parts[1])


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
