"""tmux session management — start, stop, list, query, graceful close."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Sequence

from agent_backbone.services.terminal._core import _run_tmux, session_exists

log = logging.getLogger(__name__)


def _default_shell() -> list[str]:
    """The login shell tmux starts when given no command.

    Spelled out explicitly because scrubbing and exporting variables needs an
    ``env`` prefix, and ``env`` needs something to exec. ``-l`` keeps tmux's
    own semantics: it runs ``default-shell`` as a login shell, so the user's
    profile (and the PATH it sets) still applies.
    """
    return [os.environ.get("SHELL") or "/bin/sh", "-l"]


SESSION_FORMAT_STR = "pane_in_mode=#{pane_in_mode}\nclient_activity=#{client_activity}"
"""Format variables the readiness check asks tmux for."""


async def query_environment_var(session_name: str, key: str) -> str | None:
    """Read one variable from a session's tmux environment."""
    rc, stdout, _ = await _run_tmux(
        "show-environment", "-t", session_name, key, capture_stdout=True
    )
    if rc != 0:
        return None
    value = stdout.decode().strip()
    if not value or value.startswith("-"):
        return None
    _, _, env_value = value.partition("=")
    return env_value or None


async def start_session(
    session_name: str,
    working_dir: str | None = None,
    command: list[str] | None = None,
    environment: dict[str, str] | None = None,
    scrub: Sequence[str] | None = None,
) -> bool:
    """Start a detached tmux session running ``command`` (or a shell).

    ``environment`` is exported into the initial command (via ``env``) and
    into the tmux session environment (``new-session -e``), so hooks and
    later shells see it from the session's first instant.

    ``scrub`` names variables the session must **not** inherit from the tmux
    server (the backbone's secrets — issue #81). They are removed three
    ways: ``env -u`` for the process started here; ``new-session -e NAME=``
    so the session's own environment shadows the server's from the first
    instant (a pane opened before the cleanup below sees an empty value, not
    the secret); and ``set-environment -r`` so later panes and shells start
    without them at all. If that last step fails the session is killed and
    False is returned — a session that may leak is worse than no session. A
    name that ``environment`` sets explicitly is left alone: an agent's own
    configured ``env`` wins over the scrub.

    Returns True when the session exists afterwards.
    """
    if await session_exists(session_name):
        log.info("Session '%s' already exists", session_name)
        return True

    environment = environment or {}
    removals = [key for key in dict.fromkeys(scrub or ()) if key not in environment]

    args = ["new-session", "-d", "-s", session_name]
    if working_dir:
        args.extend(["-c", working_dir])
    for key, value in environment.items():
        args.extend(["-e", f"{key}={value}"])
    for key in removals:
        args.extend(["-e", f"{key}="])
    launch = list(command) if command else _default_shell()
    if environment or removals:
        args.append("env")
        args.extend(f"-u{key}" for key in removals)
        args.extend(f"{key}={value}" for key, value in environment.items())
    args.extend(launch)

    rc, _, stderr = await _run_tmux(*args)
    if rc != 0:
        log.error("Failed to start session '%s': %s", session_name, stderr.decode())
        return False
    log.info("Started tmux session '%s'", session_name)

    for key in removals:
        # -r: remove the variable from the environment before starting a
        # process, so a new pane in this session never sees it either.
        rc, _, stderr = await _run_tmux("set-environment", "-t", session_name, "-r", key)
        if rc != 0:
            log.error(
                "Could not scrub '%s' from session '%s' (%s); killing the session",
                key,
                session_name,
                stderr.decode().strip(),
            )
            await _run_tmux("kill-session", "-t", session_name)
            return False
    return True


async def stop_session(session_name: str) -> bool:
    """Kill a tmux session. True when the session is gone afterwards."""
    if not await session_exists(session_name):
        log.info("Session '%s' does not exist", session_name)
        return True
    rc, _, stderr = await _run_tmux("kill-session", "-t", session_name)
    if rc != 0:
        log.error("Failed to stop session '%s': %s", session_name, stderr.decode())
        return False
    log.info("Stopped tmux session '%s'", session_name)
    return True


async def list_sessions() -> list[str]:
    """Names of every active tmux session."""
    rc, stdout, _ = await _run_tmux("list-sessions", "-F", "#{session_name}", capture_stdout=True)
    if rc != 0:
        return []
    return [s.strip() for s in stdout.decode().splitlines() if s.strip()]


async def query_format_vars(
    session_name: str, format_str: str = SESSION_FORMAT_STR
) -> dict[str, str]:
    """``key=value`` lines from ``tmux display-message -p`` for a session."""
    rc, stdout, _ = await _run_tmux(
        "display-message", "-p", "-t", session_name, format_str, capture_stdout=True
    )
    if rc != 0:
        return {}
    result: dict[str, str] = {}
    for line in stdout.decode().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep:
            result[key.strip()] = value.strip()
    return result


async def list_sessions_rich() -> list[dict]:
    """Sessions with metadata: name, windows, created, attached, activity."""
    fmt = (
        "#{session_name}\t#{session_windows}\t#{session_created}"
        "\t#{session_attached}\t#{session_activity}"
    )
    rc, stdout, _ = await _run_tmux("list-sessions", "-F", fmt, capture_stdout=True)
    if rc != 0:
        return []
    results: list[dict] = []
    for line in stdout.decode().splitlines():
        parts = line.strip().split("\t")
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
    """SIGTERM the pane's foreground process, wait, then kill the session.

    Used to stop the backbone's own detached session. True when the session
    is gone afterwards.
    """
    pid_str = (await query_format_vars(session_name, "pane_pid=#{pane_pid}")).get("pane_pid", "")
    if not pid_str.isdigit():
        log.warning("Could not get pane_pid for '%s', falling back to kill", session_name)
        return await stop_session(session_name)

    pid = int(pid_str)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        log.info("Process %d already gone for '%s'", pid, session_name)
        return not await session_exists(session_name) or await stop_session(session_name)
    except OSError as exc:
        log.warning("Failed to signal PID %d for '%s': %s", pid, session_name, exc)
        return await stop_session(session_name)

    poll_interval = 0.5
    elapsed = 0.0
    while elapsed < timeout:
        dead_vars = await query_format_vars(session_name, "pane_dead=#{pane_dead}")
        if dead_vars.get("pane_dead") == "1":
            log.info("Process exited gracefully in '%s'", session_name)
            return await stop_session(session_name)
        if not await session_exists(session_name):
            return True
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    log.warning("Graceful close timed out for '%s', killing session", session_name)
    return await stop_session(session_name)
