"""Agent session operations — start/stop configured agents in tmux."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from agent_backbone.services.terminal import (
    RUNTIME_ENV_KEY,
    list_sessions,
    session_exists,
    start_session,
    stop_session,
)

if TYPE_CHECKING:
    from agent_backbone.config import AgentSpec, BackboneConfig

log = logging.getLogger(__name__)

# Fallback directories for binaries not on PATH (common for npm/bun global installs)
_FALLBACK_DIRS = (
    Path.home() / ".bun" / "bin",
    Path.home() / ".local" / "bin",
    Path.home() / ".npm-global" / "bin",
)

RUNTIME_COMMANDS: dict[str, str | None] = {
    "claude": "claude",
    "gemini": "gemini",
    "codex": "codex",
    "cursor": "cursor",
    "opencode": "opencode",
    "aider": "aider",
    "shell": None,
}

RUNTIME_DISPLAY_NAMES: dict[str, str] = {
    "claude": "Claude Code",
    "gemini": "Gemini CLI",
    "codex": "Codex",
    "cursor": "Cursor Agent",
    "opencode": "OpenCode",
    "aider": "Aider",
    "shell": "Plain shell",
}


def resolve_command(name: str | None) -> str | None:
    """Resolve a command name to an absolute path (PATH first, then fallbacks)."""
    if name is None:
        return None
    path = shutil.which(name)
    if path:
        return path
    for directory in _FALLBACK_DIRS:
        candidate = directory / name
        if candidate.is_file():
            return str(candidate)
    return None


def runtime_available(runtime: str) -> bool:
    """Whether the runtime's binary is installed (shell is always available)."""
    if runtime not in RUNTIME_COMMANDS:
        return False
    command = RUNTIME_COMMANDS[runtime]
    return command is None or resolve_command(command) is not None


def build_command(
    runtime: str, *, model: str | None = None, resume: bool = False
) -> list[str] | None:
    """Build the launch command for a runtime, or None for a plain shell.

    Raises ValueError for unknown runtimes and RuntimeError when the binary is missing.
    """
    if runtime not in RUNTIME_COMMANDS:
        raise ValueError(f"Unknown runtime: {runtime}")
    binary = RUNTIME_COMMANDS[runtime]
    if binary is None:
        return None
    resolved = resolve_command(binary)
    if resolved is None:
        raise RuntimeError(f"Runtime '{runtime}' binary not found: {binary}")
    command = [resolved]
    if model:
        command.extend(["--model", model])
    if resume:
        command.append("--resume")
    return command


async def start_agent(
    spec: AgentSpec,
    *,
    runtime: str | None = None,
    model: str | None = None,
    resume: bool = False,
) -> bool:
    """Start a configured agent in its tmux session (idempotent)."""
    if await session_exists(spec.name):
        log.info("Agent '%s' already running", spec.name)
        return True

    if not spec.path.is_dir():
        log.error("Directory '%s' does not exist for agent '%s'", spec.path, spec.name)
        return False

    effective_runtime = runtime or spec.runtime
    effective_model = model if model is not None else spec.model
    try:
        command = build_command(effective_runtime, model=effective_model, resume=resume)
    except (ValueError, RuntimeError) as exc:
        log.error("Cannot start agent '%s': %s", spec.name, exc)
        return False

    environment = {RUNTIME_ENV_KEY: effective_runtime, **spec.env}
    ok = await start_session(
        spec.name,
        working_dir=str(spec.path),
        command=command,
        environment=environment,
    )
    if ok:
        extra = f", model: {effective_model}" if effective_model else ""
        log.info("Agent '%s' started (runtime: %s%s)", spec.name, effective_runtime, extra)
    return ok


async def stop_agent(name: str) -> bool:
    """Stop a single agent session."""
    ok = await stop_session(name)
    if ok:
        log.info("Agent '%s' stopped", name)
    return ok


async def start_group(specs: Iterable[AgentSpec], **overrides) -> int:
    """Start several agents. Returns the count of newly started sessions."""
    started = 0
    for spec in specs:
        if await session_exists(spec.name):
            continue
        if await start_agent(spec, **overrides):
            started += 1
    return started


async def start_all(config: BackboneConfig, **overrides) -> int:
    """Start every configured agent."""
    return await start_group(list(config.agents), **overrides)


async def stop_all_agents(config: BackboneConfig) -> int:
    """Stop every running session that belongs to a configured agent."""
    sessions = await list_sessions()
    stopped = 0
    for session in sessions:
        if session not in config.agents:
            continue
        if await stop_session(session):
            stopped += 1
    log.info("Stopped %d agent session(s)", stopped)
    return stopped


def list_agents(config: BackboneConfig) -> str:
    """Format a list of all configured agents with their directories."""
    if not config.agents:
        return "No agents configured. Add [agents.<name>] tables to backbone.toml."
    width = max(len(name) for name in config.agents.names)
    lines = []
    for spec in config.agents:
        model = f" ({spec.model})" if spec.model else ""
        lines.append(f"  {spec.name:<{width}s}  {spec.runtime}{model}  {spec.path}")
    return "\n".join(lines)
