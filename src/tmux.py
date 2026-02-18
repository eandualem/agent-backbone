"""Async tmux operations.

Provides non-blocking tmux session checks and message delivery.
Uses asyncio.create_subprocess_exec instead of subprocess.run
to avoid blocking the event loop in Prefect flows.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

log = logging.getLogger(__name__)

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
) -> bool:
    """Start a new detached tmux session.

    Args:
        session_name: Name for the tmux session.
        working_dir: Starting directory for the session.
        command: Shell command to run in the session (e.g. "claude").
        apply_theme: Whether to apply the tmux theme script after creation.

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
