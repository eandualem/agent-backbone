"""Live tmux/process observation helpers for agent state."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_backbone.services.agents.models import AgentState, StateSnapshot
from agent_backbone.services.terminal._adapters import (
    TerminalRuntime,
    detect_runtime_from_pane,
    sanitize_pane_content,
)
from agent_backbone.services.terminal._core import capture_pane, session_exists
from agent_backbone.services.terminal._sessions import query_format_vars

log = logging.getLogger(__name__)

_INFRASTRUCTURE_PROCESS_PREFIXES = ("docker", "containerd", "com.docker.", "caffeinate")
_TEAMMATE_ARG_MARKERS = ("--agent-id", "--parent-session-id")
_PERMISSION_CAPTURE_LINES = 40
_PROMPT_LINE_PATTERNS = (
    re.compile(r"\bdo you want to proceed\??$", re.IGNORECASE),
    re.compile(r"\bpress enter to confirm\b", re.IGNORECASE),
)
_PROMPT_HINT_PATTERNS = (
    re.compile(r"\byes,\s*(?:proceed|accept edits|allow)\b", re.IGNORECASE),
    re.compile(r"\bpress enter to confirm\b", re.IGNORECASE),
    re.compile(r"\b(?:esc|escape)\s+to\s+cancel\b", re.IGNORECASE),
    re.compile(r"\bno,\s*(?:cancel|deny)\b", re.IGNORECASE),
)
_TOOL_LINE_RE = re.compile(
    r"^(?P<tool>Read|Write|Edit|MultiEdit|Bash|Shell)\b(?:\s*[:|-]\s*|\s+)?(?P<rest>.*)$",
    re.IGNORECASE,
)
_FIELD_LINE_RE = re.compile(
    (
        r"^(?P<label>tool|action|command|path|target|working directory|directory|cwd)"
        r"\s*:\s*(?P<value>.+)$"
    ),
    re.IGNORECASE,
)
_LEADING_CHROME = "│┃┆┊•●·>:- "
_COMMAND_PREFIXES = (
    "$ ",
    "git ",
    "uv ",
    "python ",
    "pytest ",
    "make ",
    "bash ",
    "sh ",
    "zsh ",
    "npm ",
    "pnpm ",
    "yarn ",
    "docker ",
    "tmux ",
)
_STATUS_LINE_FRAGMENTS = (
    "accept edits on",
    "context left",
    "messages to be submitted after next tool call",
    "for shortcuts",
)


@dataclass(frozen=True)
class SessionObservation:
    """Observed tmux/process facts for one session."""

    session: str
    online: bool
    pane_pid: int | None = None
    has_sub_agent_processes: bool = False
    permission_request: dict[str, Any] | None = None


@dataclass(frozen=True)
class ProcessDescendant:
    """One descendant process under the tmux pane process."""

    pid: int
    ppid: int
    comm: str
    command: str


@dataclass(frozen=True)
class PermissionRequest:
    """Structured permission request parsed from the visible terminal prompt."""

    tool: str
    prompt: str
    target: str | None = None
    command: str | None = None
    cwd: str | None = None

    def as_context(self) -> dict[str, Any]:
        context: dict[str, Any] = {
            "tool": self.tool,
            "prompt": self.prompt,
        }
        if self.target:
            context["target"] = self.target
        if self.command:
            context["command"] = self.command
        if self.cwd:
            context["cwd"] = self.cwd
        return context


async def _list_child_pids(parent_pid: int) -> list[int]:
    """Return direct child PIDs for a process."""
    proc = await asyncio.create_subprocess_exec(
        "pgrep",
        "-P",
        str(parent_pid),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return []
    return [int(pid) for pid in stdout.decode().splitlines() if pid.strip().isdigit()]


async def _list_descendant_processes(parent_pid: int | None) -> list[ProcessDescendant]:
    """Return all descendant processes below the pane process."""
    if parent_pid is None:
        return []

    descendant_pids: list[int] = []
    queue = [parent_pid]
    seen: set[int] = {parent_pid}

    while queue:
        current_pid = queue.pop(0)
        for child_pid in await _list_child_pids(current_pid):
            if child_pid in seen:
                continue
            seen.add(child_pid)
            descendant_pids.append(child_pid)
            queue.append(child_pid)

    if not descendant_pids:
        return []

    proc = await asyncio.create_subprocess_exec(
        "ps",
        "-o",
        "pid=",
        "-o",
        "ppid=",
        "-o",
        "comm=",
        "-o",
        "args=",
        "-p",
        ",".join(str(pid) for pid in descendant_pids),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return []

    descendants: list[ProcessDescendant] = []
    for line in stdout.decode().splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        pid_raw, ppid_raw, comm, command = parts
        if not pid_raw.isdigit() or not ppid_raw.isdigit():
            continue
        descendants.append(
            ProcessDescendant(
                pid=int(pid_raw),
                ppid=int(ppid_raw),
                comm=comm.strip(),
                command=command.strip(),
            )
        )
    return descendants


def _process_executable(command: str, comm: str) -> str:
    """Best-effort executable name for a process row."""
    if command.strip():
        return Path(command.strip().split()[0]).name.lower()
    return Path(comm.strip()).name.lower()


def _is_infrastructure_process(command: str) -> bool:
    """Whether a child command is infrastructure noise, not a sub-agent."""
    executable = _process_executable(command, command)
    return executable.startswith(_INFRASTRUCTURE_PROCESS_PREFIXES)


def _is_cli_process(process: ProcessDescendant) -> bool:
    """Whether a process row looks like a Claude Code or Codex CLI process."""
    executable = _process_executable(process.command, process.comm)
    if executable in {"codex", "claude"}:
        return True
    return executable == "node" and "claude" in process.command.lower()


def _has_teammate_marker(process: ProcessDescendant) -> bool:
    """Whether a process argv carries the positive teammate identifiers."""
    lowered = process.command.lower()
    return any(marker in lowered for marker in _TEAMMATE_ARG_MARKERS)


def _find_sub_agent_processes(
    descendants: list[ProcessDescendant],
) -> list[ProcessDescendant]:
    """Return descendant CLI processes that positively identify as teammates."""
    cli_processes = [process for process in descendants if _is_cli_process(process)]
    teammate_processes = [
        process for process in cli_processes if _has_teammate_marker(process)
    ]
    if teammate_processes:
        return teammate_processes

    if len(cli_processes) > 1:
        log.debug(
            "Ambiguous extra CLI descendants treated as idle: %s",
            [process.command for process in cli_processes],
        )
    return []


def _normalize_tool_name(tool: str) -> str:
    normalized = tool.strip()
    lowered = normalized.lower()
    aliases = {
        "multiedit": "MultiEdit",
        "shell": "Shell",
        "bash": "Bash",
        "read": "Read",
        "write": "Write",
        "edit": "Edit",
    }
    return aliases.get(lowered, normalized)


def _normalize_pane_line(line: str) -> str:
    stripped = sanitize_pane_content(line).strip()
    if not stripped:
        return ""
    stripped = stripped.lstrip(_LEADING_CHROME).strip()
    if not stripped:
        return ""
    return " ".join(stripped.split())


def _permission_tail_lines(pane_content: str) -> list[str]:
    normalized = [_normalize_pane_line(line) for line in pane_content.splitlines()]
    return [line for line in normalized if line][-15:]


def _is_status_line(line: str) -> bool:
    lowered = line.lower()
    return any(fragment in lowered for fragment in _STATUS_LINE_FRAGMENTS)


def _looks_like_path(value: str) -> bool:
    candidate = value.strip()
    return candidate.startswith(("/", "~/", "./", "../")) or "/" in candidate


def _looks_like_command(value: str) -> bool:
    candidate = value.strip()
    lowered = candidate.lower()
    return candidate.startswith("$ ") or lowered.startswith(_COMMAND_PREFIXES)


def _strip_command_prefix(value: str) -> str:
    command = value.strip()
    if command.startswith("$ "):
        command = command[2:]
    return command.strip()


def _find_prompt_line(lines: list[str]) -> tuple[int, str] | None:
    for idx in range(len(lines) - 1, -1, -1):
        line = lines[idx]
        if any(pattern.search(line) for pattern in _PROMPT_LINE_PATTERNS):
            return idx, line
    for idx in range(len(lines) - 1, -1, -1):
        line = lines[idx]
        if any(pattern.search(line) for pattern in _PROMPT_HINT_PATTERNS):
            return idx, line
    return None


def _extract_permission_details(
    lines: list[str],
    *,
    runtime: TerminalRuntime,
) -> tuple[str | None, str | None, str | None, str | None]:
    del runtime

    tool: str | None = None
    target: str | None = None
    command: str | None = None
    cwd: str | None = None

    for line in reversed(lines):
        if _is_status_line(line):
            continue

        field_match = _FIELD_LINE_RE.match(line)
        if field_match:
            label = field_match.group("label").lower()
            value = field_match.group("value").strip()
            if label in {"tool", "action"} and tool is None:
                tool = _normalize_tool_name(value)
            elif label == "command" and command is None:
                command = _strip_command_prefix(value)
            elif label in {"path", "target"} and target is None:
                target = value
            elif label in {"working directory", "directory", "cwd"} and cwd is None:
                cwd = value
            continue

        tool_match = _TOOL_LINE_RE.match(line)
        if tool_match:
            candidate_tool = _normalize_tool_name(tool_match.group("tool"))
            rest = tool_match.group("rest").strip()
            if tool is None:
                tool = candidate_tool
            if rest:
                if candidate_tool == "Bash":
                    command = command or _strip_command_prefix(rest)
                    target = target or command
                else:
                    target = target or rest
            continue

        if tool is None and _looks_like_command(line):
            tool = "Bash"
            command = command or _strip_command_prefix(line)
            target = target or command
            continue

        if target is None and _looks_like_path(line):
            target = line

    if tool in {"Read", "Write", "Edit", "MultiEdit"} and target is None:
        for line in reversed(lines):
            if _looks_like_path(line):
                target = line
                break

    if tool == "Bash" and command is None:
        for line in reversed(lines):
            if _looks_like_command(line):
                command = _strip_command_prefix(line)
                target = target or command
                break

    if tool is None and target is None and command is None and cwd is None:
        return None, None, None, None
    return tool, target, command, cwd


def _extract_permission_request(pane_content: str) -> dict[str, Any] | None:
    lines = _permission_tail_lines(pane_content)
    if not lines:
        return None

    prompt_match = _find_prompt_line(lines)
    if prompt_match is None:
        return None

    prompt_idx, prompt_line = prompt_match
    context_lines = [line for line in lines[max(0, prompt_idx - 8) : prompt_idx] if line]
    tool, target, command, cwd = _extract_permission_details(
        context_lines,
        runtime=detect_runtime_from_pane(pane_content),
    )
    if tool is None and target is None and command is None and cwd is None:
        return None

    request = PermissionRequest(
        tool=tool or "unknown",
        target=target,
        prompt=prompt_line,
        command=command,
        cwd=cwd,
    )
    return request.as_context()


async def observe_session(session: str) -> SessionObservation:
    """Observe whether a session is online and whether it has teammate processes."""
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
    has_sub_agent_processes = False
    try:
        descendants = await _list_descendant_processes(pane_pid)
        has_sub_agent_processes = bool(_find_sub_agent_processes(descendants))
    except Exception:
        log.exception("Failed to inspect process tree for %s", session)

    permission_request: dict[str, Any] | None = None
    if not has_sub_agent_processes:
        try:
            pane_content = await capture_pane(session, lines=_PERMISSION_CAPTURE_LINES)
            if pane_content:
                permission_request = _extract_permission_request(pane_content)
        except Exception:
            log.exception("Failed to inspect pane content for %s", session)

    return SessionObservation(
        session=session,
        online=True,
        pane_pid=pane_pid,
        has_sub_agent_processes=has_sub_agent_processes,
        permission_request=permission_request,
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
    if observation.has_sub_agent_processes:
        return StateSnapshot(
            state=AgentState.SUB_AGENT_WAITING,
            timestamp=ts,
            source="observed",
        )
    if observation.permission_request is not None:
        return StateSnapshot(
            state=AgentState.PERMISSION_WAITING,
            timestamp=ts,
            source="observed",
            context=observation.permission_request,
        )
    return StateSnapshot(state=AgentState.IDLE, timestamp=ts, source="observed")


async def enrich_idle_state(session: str, snapshot: StateSnapshot) -> StateSnapshot:
    """Upgrade an `idle` snapshot to an observed waiting state when applicable."""
    if snapshot.state != AgentState.IDLE:
        return snapshot

    observation = await observe_session(session)
    if not observation.online:
        return snapshot
    if not observation.has_sub_agent_processes:
        if observation.permission_request is None:
            return snapshot
        return StateSnapshot(
            state=AgentState.PERMISSION_WAITING,
            current_issue=snapshot.current_issue,
            timestamp=snapshot.timestamp,
            source=snapshot.source,
            started_at=snapshot.started_at,
            context=observation.permission_request,
            plan_file=snapshot.plan_file,
            plan_title=snapshot.plan_title,
        )
    return StateSnapshot(
        state=AgentState.SUB_AGENT_WAITING,
        current_issue=snapshot.current_issue,
        timestamp=snapshot.timestamp,
        source=snapshot.source,
        started_at=snapshot.started_at,
        context=snapshot.context,
        plan_file=snapshot.plan_file,
        plan_title=snapshot.plan_title,
    )
