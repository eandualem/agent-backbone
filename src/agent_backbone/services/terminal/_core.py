"""Core tmux operations — session checks, paste buffer, key sending, pane capture."""

from __future__ import annotations

import asyncio
import logging
import uuid

from agent_backbone.base import check_before_effect, owned_process
from agent_backbone.services.terminal._server import tmux_command

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
    check_effect=None,
) -> tuple[int, bytes, bytes]:
    """Run a tmux subprocess with concurrency limiting.

    Returns (returncode, stdout, stderr). stdout is only captured
    when capture_stdout=True; otherwise it's empty bytes.
    When stdin_data is provided, it is piped to the process's stdin.
    """
    command = tmux_command(*args)
    async with _get_semaphore():
        await check_before_effect(check_effect)
        kwargs: dict = {
            "stdout": asyncio.subprocess.PIPE if capture_stdout else asyncio.subprocess.DEVNULL,
            "stderr": asyncio.subprocess.PIPE,
        }
        if stdin_data is not None:
            kwargs["stdin"] = asyncio.subprocess.PIPE
        async with owned_process(asyncio.create_subprocess_exec(*command, **kwargs)) as proc:
            try:
                # Argument-based commands were admitted at spawn. Only stdin
                # commands still have an effect to authorize after creation.
                if stdin_data is not None:
                    await check_before_effect(check_effect)
                async with asyncio.timeout(10.0):
                    stdout, stderr = await proc.communicate(input=stdin_data)
            except TimeoutError:
                # Launch arguments can contain environment values and prompts.
                log.error("tmux %s timed out", args[0] if args else "command")
                return -1, b"", b"tmux command timed out"
            return proc.returncode, stdout or b"", stderr or b""


def exact_target(session_name: str) -> str:
    """The tmux target for exactly this session: ``-t name`` also accepts a
    prefix, so an offline ``app`` would resolve to ``app-2``.

    ``=name`` alone is only a *session* target: tmux 3.7 resolves it for
    ``has-session`` and ``kill-session`` but answers ``can't find pane`` for
    ``paste-buffer``, ``send-keys``, ``capture-pane`` and ``display-message``
    (measured live). ``=name:`` — exact session, its current window and pane —
    is accepted by every command the backbone runs, so it is the one spelling.
    """
    return f"={session_name}:"


async def session_exists(session_name: str) -> bool:
    """Check if a tmux session exists."""
    rc, _, _ = await _run_tmux("has-session", "-t", exact_target(session_name))
    return rc == 0


async def press_submit(session_name: str, *, check_effect=None) -> bool:
    """Send Enter to submit the currently buffered prompt input."""
    rc, _, stderr = await _run_tmux(
        "send-keys",
        "-t",
        exact_target(session_name),
        "Enter",
        **({"check_effect": check_effect} if check_effect is not None else {}),
    )
    if rc != 0:
        log.error("tmux send-keys Enter failed for '%s': %s", session_name, stderr.decode())
        return False
    return True


async def press_escape(session_name: str) -> bool:
    """Send Escape to interrupt active work and expose queued input."""
    rc, _, stderr = await _run_tmux("send-keys", "-t", exact_target(session_name), "Escape")
    if rc != 0:
        log.error("tmux send-keys Escape failed for '%s': %s", session_name, stderr.decode())
        return False
    return True


async def paste_message(session_name: str, message: str, *, check_effect=None) -> bool:
    """Paste through the admission guard; always remove our private buffer."""
    if not await session_exists(session_name):
        log.warning("tmux session '%s' not found — notification dropped", session_name)
        return False
    await check_before_effect(check_effect)
    options = {"check_effect": check_effect} if check_effect is not None else {}
    # A named buffer per paste prevents concurrent deliveries mixing text.
    buffer = f"backbone-{uuid.uuid4().hex}"
    pasted = False
    try:
        rc, _, stderr = await _run_tmux(
            "load-buffer", "-b", buffer, "-", stdin_data=message.encode(), **options
        )
        if rc != 0:
            log.error("tmux load-buffer failed for '%s': %s", session_name, stderr.decode())
            return False
        # Bracketed paste keeps multiline input together. Recheck authority
        # after loading and waiting for capacity, before the paste subprocess.
        rc, _, stderr = await _run_tmux(
            "paste-buffer",
            "-p",
            "-b",
            buffer,
            "-t",
            exact_target(session_name),
            "-d",
            **options,
        )
        pasted = rc == 0
        if not pasted:
            log.error("tmux paste-buffer failed for '%s': %s", session_name, stderr.decode())
        return pasted
    finally:
        if not pasted:
            # Cleanup of an owned buffer must survive an admission refusal.
            await _run_tmux("delete-buffer", "-b", buffer)


async def send_keys(session_name: str, keys: str) -> bool:
    """Send a tmux key name or escape sequence (``Enter``, ``Escape``, ``[Z``) to a session."""
    if not await session_exists(session_name):
        log.warning("tmux session '%s' not found — key send dropped", session_name)
        return False

    rc, _, stderr = await _run_tmux("send-keys", "-t", exact_target(session_name), keys)
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
        exact_target(session_name),
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
        exact_target(session_name),
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
        exact_target(session_name),
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
        exact_target(session_name),
        "-p",  # output to stdout
        "-e",  # include escape sequences (colors, formatting)
        "-S",
        str(-lines),  # start N lines back
        capture_stdout=True,
    )
    if rc != 0:
        return ""
    return stdout.decode()
