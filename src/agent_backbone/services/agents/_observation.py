"""Live tmux/process observation helpers for agent state."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from agent_backbone.services.agents.models import AgentState, StateSnapshot
from agent_backbone.services.terminal._core import session_exists
from agent_backbone.services.terminal._sessions import query_format_vars

log = logging.getLogger(__name__)

_INFRASTRUCTURE_PROCESS_PREFIXES = ("docker", "containerd")


@dataclass(frozen=True)
class SessionObservation:
    """Observed tmux/process facts for one session."""

    session: str
    online: bool
    pane_pid: int | None = None
    has_child_processes: bool = False


async def _has_child_processes(parent_pid: int | None) -> bool:
    """Whether the pane process currently has child processes."""
    if parent_pid is None:
        return False

    proc = await asyncio.create_subprocess_exec(
        "pgrep",
        "-P",
        str(parent_pid),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return False

    child_pids = [
        pid.strip()
        for pid in stdout.decode().splitlines()
        if pid.strip().isdigit()
    ]
    if not child_pids:
        return False

    child_proc = await asyncio.create_subprocess_exec(
        "ps",
        "-o",
        "comm=",
        "-p",
        ",".join(child_pids),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    child_stdout, _ = await child_proc.communicate()
    if child_proc.returncode != 0:
        return True

    child_commands = [
        line.strip()
        for line in child_stdout.decode().splitlines()
        if line.strip()
    ]
    return any(not _is_infrastructure_process(command) for command in child_commands)


def _is_infrastructure_process(command: str) -> bool:
    """Whether a child command is infrastructure noise, not a sub-agent."""
    executable = Path(command.strip()).name.lower()
    return executable.startswith(_INFRASTRUCTURE_PROCESS_PREFIXES)


async def observe_session(session: str) -> SessionObservation:
    """Observe whether a session is online and whether its pane has children."""
    online = await session_exists(session)
    if not online:
        return SessionObservation(session=session, online=False)

    try:
        tmux_vars = await query_format_vars(session, "pane_pid=#{pane_pid}")
    except Exception:
        log.exception("Failed to query pane pid for %s", session)
        return SessionObservation(session=session, online=True)

    pane_pid_raw = tmux_vars.get("pane_pid", "")
    pane_pid = int(pane_pid_raw) if pane_pid_raw.isdigit() else None
    has_children = False
    try:
        has_children = await _has_child_processes(pane_pid)
    except Exception:
        log.exception("Failed to inspect child processes for %s", session)

    return SessionObservation(
        session=session,
        online=True,
        pane_pid=pane_pid,
        has_child_processes=has_children,
    )


def snapshot_from_observation(
    observation: SessionObservation,
    *,
    timestamp: float | None = None,
) -> StateSnapshot:
    """Convert a tmux/process observation into the observable state fallback."""
    ts = time.time() if timestamp is None else timestamp
    if not observation.online:
        return StateSnapshot(state=AgentState.OFFLINE, timestamp=ts, source="observed")
    if observation.has_child_processes:
        return StateSnapshot(
            state=AgentState.SUB_AGENT_WAITING,
            timestamp=ts,
            source="observed",
        )
    return StateSnapshot(state=AgentState.IDLE, timestamp=ts, source="observed")


async def enrich_idle_state(session: str, snapshot: StateSnapshot) -> StateSnapshot:
    """Upgrade an `idle` snapshot to `sub_agent_waiting` when child processes exist."""
    if snapshot.state != AgentState.IDLE:
        return snapshot

    observation = await observe_session(session)
    if not observation.online:
        return snapshot
    if not observation.has_child_processes:
        return snapshot
    return StateSnapshot(
        state=AgentState.SUB_AGENT_WAITING,
        current_issue=snapshot.current_issue,
        timestamp=snapshot.timestamp,
        source=snapshot.source,
        started_at=snapshot.started_at,
        plan_file=snapshot.plan_file,
        plan_title=snapshot.plan_title,
    )
