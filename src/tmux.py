"""Async tmux operations.

Provides non-blocking tmux session checks and message delivery.
Uses asyncio.create_subprocess_exec instead of subprocess.run
to avoid blocking the event loop in Prefect flows.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path
from typing import TypedDict

log = logging.getLogger(__name__)


class PaneInfo(TypedDict):
    pane_id: str
    pane_index: str
    pane_width: str
    pane_height: str
    pane_active: bool


class WindowInfo(TypedDict):
    window_id: str
    window_index: str
    window_name: str
    window_active: bool
    window_panes: int


# Default format string for session intelligence queries
SESSION_FORMAT_STR = "pane_in_mode=#{pane_in_mode}\nclient_activity=#{client_activity}"

# Named entity → working directory mapping
_ENTITY_DIRS: dict[str, str] = {
    "feynman": str(Path.home() / "orchestration"),
    "ike": str(Path.home() / "ws" / "core" / "ike"),
    "leo": str(Path.home() / "ws" / "leo"),
    "ada": str(Path.home() / "ws" / "core" / "spec"),
    "brunel": str(Path.home() / "infra"),
    "hamilton": str(Path.home() / "ws" / "core" / "hamilton"),
    "curie": str(Path.home() / "ws" / "core" / "curie"),
    "bell": str(Path.home() / "ws" / "core" / "bell"),
}

# Base directories to search for coding repos
_CODE_BASE_DIRS = [
    Path.home() / "ws" / "core" / "code" / "Arclio",
    Path.home() / "ws" / "core" / "code" / "Loveble",
    Path.home() / "ws" / "core" / "code" / "WF",
]


def resolve_agent_dir(session_name: str) -> str:
    """Resolve the working directory for an agent session.

    Named entities map to fixed directories. Coding repos search
    base directories for a matching folder name.

    Returns empty string if unresolvable.
    """
    # Named entities
    if session_name in _ENTITY_DIRS:
        return _ENTITY_DIRS[session_name]

    # Coding repos: check base dirs for a matching folder
    for base in _CODE_BASE_DIRS:
        candidate = base / session_name
        if candidate.is_dir():
            return str(candidate)

    return ""


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


async def start_session(
    session_name: str,
    working_dir: str | None = None,
    command: str | None = None,
    apply_theme: bool = True,
    environment: dict[str, str] | None = None,
) -> bool:
    """Start a new detached tmux session.

    Args:
        session_name: Name for the tmux session.
        working_dir: Starting directory for the session.
        command: Shell command to run in the session (e.g. "claude").
        apply_theme: Whether to apply the tmux theme script after creation.
        environment: Optional dict of environment variables to set in the session.

    Returns True if the session was created, False if it already exists or failed.
    """
    if await session_exists(session_name):
        log.info("Session '%s' already exists", session_name)
        return True

    args = ["tmux", "new-session", "-d", "-s", session_name]
    if working_dir:
        args.extend(["-c", working_dir])
    if command:
        args.append(command)

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

    # Apply theme (fire-and-forget, non-critical)
    if apply_theme:
        theme_script = Path.home() / "orchestration" / "tmux" / "hooks" / "apply-theme.sh"
        if theme_script.is_file():
            try:
                theme_proc = await asyncio.create_subprocess_exec(
                    str(theme_script),
                    session_name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await theme_proc.wait()
            except Exception:
                log.debug("Theme application failed for '%s' (non-critical)", session_name)

    # Set environment variables
    if environment:
        for key, value in environment.items():
            env_proc = await asyncio.create_subprocess_exec(
                "tmux",
                "set-environment",
                "-t",
                session_name,
                key,
                value,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, env_stderr = await env_proc.communicate()
            if env_proc.returncode != 0:
                log.warning(
                    "Failed to set env var '%s' for session '%s': %s",
                    key,
                    session_name,
                    env_stderr.decode(),
                )

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


async def query_format_vars(
    session_name: str, format_str: str = SESSION_FORMAT_STR
) -> dict[str, str]:
    """Query tmux format variables for a session.

    Runs `tmux display-message -p -t {session} '{format_str}'` and parses
    key=value lines from the output. Returns empty dict on error or missing session.
    """
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "display-message",
        "-p",
        "-t",
        session_name,
        format_str,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return {}

    result: dict[str, str] = {}
    for line in stdout.decode().splitlines():
        line = line.strip()
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


async def start_pipe_pane(session_name: str, output_path: str) -> bool:
    """Start piping pane output to a file.

    Runs `tmux pipe-pane -t {session} -o 'cat >> {path}'`.
    Returns True if the command succeeded, False otherwise.
    """
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "pipe-pane",
        "-t",
        session_name,
        "-o",
        f"cat >> {output_path}",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("pipe-pane start failed for '%s': %s", session_name, stderr.decode())
        return False
    log.info("Started pipe-pane for '%s' → %s", session_name, output_path)
    return True


async def stop_pipe_pane(session_name: str) -> bool:
    """Stop piping pane output.

    Runs `tmux pipe-pane -t {session}` (no -o disconnects the pipe).
    Returns True if the command succeeded, False otherwise.
    """
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "pipe-pane",
        "-t",
        session_name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("pipe-pane stop failed for '%s': %s", session_name, stderr.decode())
        return False
    log.info("Stopped pipe-pane for '%s'", session_name)
    return True


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


async def list_panes(session_name: str) -> list[PaneInfo]:
    """List panes in a tmux session with metadata."""
    fmt = "#{pane_id}\t#{pane_index}\t#{pane_width}\t#{pane_height}\t#{pane_active}"
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "list-panes",
        "-t",
        session_name,
        "-F",
        fmt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return []
    results: list[PaneInfo] = []
    for line in stdout.decode().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        results.append(
            PaneInfo(
                pane_id=parts[0],
                pane_index=parts[1],
                pane_width=parts[2],
                pane_height=parts[3],
                pane_active=parts[4] == "1",
            )
        )
    return results


async def split_pane(
    session_name: str,
    *,
    direction: str = "vertical",
    size: str | None = None,
    command: str | None = None,
    target_pane: str | None = None,
) -> bool:
    """Split a pane in a tmux session.

    Args:
        session_name: Target tmux session.
        direction: 'vertical' (-v) or 'horizontal' (-h).
        size: Size for the new pane (e.g. '50%', '20').
        command: Shell command to run in the new pane.
        target_pane: Specific pane to split (e.g. '0'). Defaults to active pane.
    """
    target = f"{session_name}:{target_pane}" if target_pane else session_name
    args = ["tmux", "split-window"]
    args.append("-v" if direction == "vertical" else "-h")
    if size:
        args.extend(["-l", size])
    args.extend(["-t", target])
    if command:
        args.append(command)

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("split-pane failed for '%s': %s", session_name, stderr.decode())
        return False
    return True


async def resize_pane(
    session_name: str,
    pane_id: str,
    *,
    width: int | None = None,
    height: int | None = None,
) -> bool:
    """Resize a pane in a tmux session.

    At least one of width or height must be provided.
    """
    args = ["tmux", "resize-pane", "-t", f"{session_name}:{pane_id}"]
    if width is not None:
        args.extend(["-x", str(width)])
    if height is not None:
        args.extend(["-y", str(height)])

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("resize-pane failed for '%s:%s': %s", session_name, pane_id, stderr.decode())
        return False
    return True


async def swap_panes(session_name: str, pane_a: str, pane_b: str) -> bool:
    """Swap two panes in a tmux session."""
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "swap-pane",
        "-s",
        f"{session_name}:{pane_a}",
        "-t",
        f"{session_name}:{pane_b}",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("swap-pane failed for '%s': %s", session_name, stderr.decode())
        return False
    return True


async def close_pane(session_name: str, pane_id: str) -> bool:
    """Close (kill) a pane in a tmux session."""
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "kill-pane",
        "-t",
        f"{session_name}:{pane_id}",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("close-pane failed for '%s:%s': %s", session_name, pane_id, stderr.decode())
        return False
    return True


async def set_layout(session_name: str, layout: str) -> bool:
    """Apply a tmux layout preset to a session.

    Common layouts: 'even-horizontal', 'even-vertical', 'main-horizontal',
    'main-vertical', 'tiled'.
    """
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "select-layout",
        "-t",
        session_name,
        layout,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("set-layout failed for '%s': %s", session_name, stderr.decode())
        return False
    return True


async def create_layout(
    session_name: str,
    layout_spec: dict,
) -> bool:
    """Create a multi-pane layout from a spec.

    Args:
        session_name: Target tmux session.
        layout_spec: Dict with keys:
            - panes (int): Number of panes (must be >= 1).
            - layout (str): Layout preset to apply after splitting.
            - commands (list[str] | None): Optional commands to send to each pane.

    Creates panes by splitting N-1 times, applies the layout preset,
    then optionally sends commands to each pane.
    """
    pane_count = layout_spec.get("panes", 1)
    layout = layout_spec.get("layout", "tiled")
    commands = layout_spec.get("commands")

    # Split N-1 times to create the desired number of panes
    for _ in range(pane_count - 1):
        if not await split_pane(session_name):
            log.error("create_layout: split failed for '%s', aborting", session_name)
            return False

    # Apply layout preset
    if not await set_layout(session_name, layout):
        log.error("create_layout: set_layout failed for '%s'", session_name)
        return False

    # Send commands to panes if provided
    if commands:
        panes = await list_panes(session_name)
        for i, cmd in enumerate(commands):
            if i < len(panes) and cmd:
                await send_message(f"{session_name}:{panes[i]['pane_index']}", cmd)

    return True


async def create_window(
    session_name: str,
    *,
    name: str | None = None,
    command: str | None = None,
) -> bool:
    """Create a new window in a tmux session."""
    args = ["tmux", "new-window", "-t", session_name]
    if name:
        args.extend(["-n", name])
    if command:
        args.append(command)

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("create-window failed for '%s': %s", session_name, stderr.decode())
        return False
    return True


async def close_window(session_name: str, window_id: str) -> bool:
    """Close (kill) a window in a tmux session."""
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "kill-window",
        "-t",
        f"{session_name}:{window_id}",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("close-window failed for '%s:%s': %s", session_name, window_id, stderr.decode())
        return False
    return True


async def rename_window(session_name: str, window_id: str, new_name: str) -> bool:
    """Rename a window in a tmux session."""
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "rename-window",
        "-t",
        f"{session_name}:{window_id}",
        new_name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("rename-window failed for '%s:%s': %s", session_name, window_id, stderr.decode())
        return False
    return True


async def list_windows(session_name: str) -> list[WindowInfo]:
    """List windows in a tmux session with metadata."""
    fmt = "#{window_id}\t#{window_index}\t#{window_name}\t#{window_active}\t#{window_panes}"
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "list-windows",
        "-t",
        session_name,
        "-F",
        fmt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return []
    results: list[WindowInfo] = []
    for line in stdout.decode().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        results.append(
            WindowInfo(
                window_id=parts[0],
                window_index=parts[1],
                window_name=parts[2],
                window_active=parts[3] == "1",
                window_panes=int(parts[4]) if parts[4].isdigit() else 0,
            )
        )
    return results


async def select_window(session_name: str, window_id: str) -> bool:
    """Select (focus) a window in a tmux session."""
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "select-window",
        "-t",
        f"{session_name}:{window_id}",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("select-window failed for '%s:%s': %s", session_name, window_id, stderr.decode())
        return False
    return True


async def graceful_close(session_name: str, timeout: float = 30.0) -> bool:
    """Gracefully close a tmux session by signaling the foreground process.

    Sends SIGTERM to the pane's foreground process, then polls for exit.
    Falls back to kill-session if the process doesn't exit within timeout.

    Returns True if the session is gone after this call, False on error.
    """
    # Get foreground PID
    vars_result = await query_format_vars(session_name, "pane_pid=#{pane_pid}")
    pid_str = vars_result.get("pane_pid", "")
    if not pid_str or not pid_str.isdigit():
        log.warning("Could not get pane_pid for '%s', falling back to kill", session_name)
        return await stop_session(session_name)

    pid = int(pid_str)

    # Send SIGTERM
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        log.info("Process %d already gone for '%s'", pid, session_name)
        # Session may still exist even if process is gone
        if not await session_exists(session_name):
            return True
        return await stop_session(session_name)
    except OSError as e:
        log.warning("Failed to signal PID %d for '%s': %s", pid, session_name, e)
        return await stop_session(session_name)

    # Poll for pane_dead
    elapsed = 0.0
    poll_interval = 0.5
    while elapsed < timeout:
        dead_vars = await query_format_vars(session_name, "pane_dead=#{pane_dead}")
        if dead_vars.get("pane_dead") == "1":
            log.info("Process exited gracefully in '%s'", session_name)
            # Clean up the session
            return await stop_session(session_name)
        if not await session_exists(session_name):
            log.info("Session '%s' gone during graceful close", session_name)
            return True
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    # Timeout — force kill
    log.warning("Graceful close timed out for '%s', killing session", session_name)
    return await stop_session(session_name)
