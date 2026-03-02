"""Tmux session management — start, stop, list, query, graceful close."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.services.registry import EntityRegistry

from agent_backbone.services.tmux._core import session_exists

log = logging.getLogger(__name__)


# Default format string for session intelligence queries
SESSION_FORMAT_STR = "pane_in_mode=#{pane_in_mode}\nclient_activity=#{client_activity}"


def resolve_agent_dir(session_name: str, registry: EntityRegistry | None = None) -> str:
    """Resolve the working directory for an agent session.

    Named entities resolve via registry home dirs. Coding repos resolve
    via registry repo paths. Returns empty string if unresolvable.
    """
    if registry is None:
        return ""

    # Named entities: look up home dir by session name
    home = registry.home_by_session.get(session_name)
    if home:
        return home

    # Coding repos: look up path by repo name
    repo_path = registry.repo_path_by_name.get(session_name)
    if repo_path:
        return repo_path

    return ""


async def start_session(
    session_name: str,
    working_dir: str | None = None,
    command: list[str] | None = None,
    apply_theme: bool = True,
    environment: dict[str, str] | None = None,
) -> bool:
    """Start a new detached tmux session.

    Args:
        session_name: Name for the tmux session.
        working_dir: Starting directory for the session.
        command: Command args to run in the session (e.g. ["claude", "--resume"]).
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
        args.extend(command)

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


async def list_sessions_rich() -> list[dict]:
    """List sessions with metadata: name, windows, created, attached, activity."""
    fmt = (
        "#{session_name}\t#{session_windows}\t#{session_created}"
        "\t#{session_attached}\t#{session_activity}"
    )
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
                "activity": int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0,
            }
        )
    return results


async def graceful_close(session_name: str, timeout: float = 30.0) -> bool:
    """Gracefully close a tmux session by signaling the foreground process.

    Sends SIGTERM to the pane's foreground process, then polls for exit.
    Falls back to kill-session if the process doesn't exit within timeout.

    Returns True if the session is gone after this call, False on error.
    """
    # Get foreground PID (with timeout to prevent indefinite hang)
    try:
        vars_result = await asyncio.wait_for(
            query_format_vars(session_name, "pane_pid=#{pane_pid}"),
            timeout=5.0,
        )
    except TimeoutError:
        log.warning("Timed out querying pane_pid for '%s', falling back to kill", session_name)
        return await stop_session(session_name)
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

    # Poll for pane_dead (with per-query timeout to prevent indefinite hang)
    elapsed = 0.0
    poll_interval = 0.5
    while elapsed < timeout:
        try:
            dead_vars = await asyncio.wait_for(
                query_format_vars(session_name, "pane_dead=#{pane_dead}"),
                timeout=5.0,
            )
        except TimeoutError:
            log.warning("Timed out polling pane_dead for '%s', falling back to kill", session_name)
            return await stop_session(session_name)
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
