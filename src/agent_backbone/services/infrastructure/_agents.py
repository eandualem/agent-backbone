"""Agent session operations — start/stop configured agents in tmux."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

from agent_backbone.services.terminal import (
    AGENT_ENV_KEY,
    RUNTIME_ENV_KEY,
    STATE_DIR_ENV_KEY,
    capture_pane,
    get_terminal_adapter,
    sanitize_pane_content,
    session_exists,
    start_session,
    stop_session,
)

if TYPE_CHECKING:
    from agent_backbone.config import AgentSpec

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


def pre_trust_directory(directory: Path | str, *, claude_config: Path | None = None) -> bool:
    """Mark a directory as trusted in Claude Code's per-project state.

    Writes the same record the interactive folder-trust dialog writes
    (``projects.<dir>.hasTrustDialogAccepted`` in ``~/.claude.json``), so a
    backbone-started session reaches its prompt without a human attaching.
    Starting an agent in a directory is a deliberate act by the owner or an
    authorized agent — that decision replaces the dialog. The write is
    best-effort: on any error the dialog simply appears as before.
    """
    path = str(Path(directory).expanduser().resolve())
    config_file = claude_config or (Path.home() / ".claude.json")
    try:
        data = json.loads(config_file.read_text()) if config_file.is_file() else {}
        if not isinstance(data, dict):
            return False
        projects = data.setdefault("projects", {})
        entry = projects.setdefault(path, {})
        if entry.get("hasTrustDialogAccepted") is True:
            return True
        entry["hasTrustDialogAccepted"] = True
        tmp = config_file.with_suffix(".json.backbone-tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(config_file)
        log.info("Pre-trusted %s for Claude Code", path)
        return True
    except (OSError, ValueError):
        log.warning("Could not pre-trust %s (the trust dialog will appear)", path)
        return False


def agent_brief_file(
    name: str, repo: str, data_dir: Path | str, *, runtime: str = "claude"
) -> Path | None:
    """Render the common backbone brief for a claude agent, return its path.

    The brief is appended to Claude Code's system prompt at launch
    (complementing the project's CLAUDE.md, never replacing it). Runtimes
    without system-prompt injection get None. Best-effort: on any error the
    agent simply starts without the brief.
    """
    if runtime != "claude":
        return None
    from agent_backbone.help import render_agent_brief

    try:
        briefs_dir = Path(data_dir) / "briefs"
        briefs_dir.mkdir(parents=True, exist_ok=True)
        brief = briefs_dir / f"{name}.md"
        brief.write_text(
            render_agent_brief(
                {"agent_name": name, "repo": repo or "(no GitHub remote)"},
                data_dir=Path(data_dir),
            )
        )
        return brief
    except OSError as exc:
        log.warning("Could not write the agent brief for %s: %s", name, exc)
        return None


def hook_launch_args(
    runtime: str, data_dir: Path | str | None, state_dir: Path | str | None
) -> list[str]:
    """Extra CLI args that wire the runtime's state hooks to the backbone.

    Claude Code accepts an additional settings file via ``--settings``; the
    backbone keeps one under ``<data_dir>/hooks/`` so every session it starts
    reports state without the user configuring hooks per repository (or at
    all). Runtimes without hook support get no extra args and rely on
    terminal inference.
    """
    if runtime != "claude" or data_dir is None or state_dir is None:
        return []
    from agent_backbone.hooks.install import ensure_launch_settings

    try:
        settings = ensure_launch_settings(Path(data_dir), Path(state_dir))
    except OSError as exc:
        log.warning("Could not write the launch hook settings: %s", exc)
        return []
    return ["--settings", str(settings)]


def build_command(
    runtime: str,
    *,
    model: str | None = None,
    resume: bool = False,
    data_dir: Path | str | None = None,
    state_dir: Path | str | None = None,
    system_prompt_file: Path | str | None = None,
) -> list[str] | None:
    """Build the launch command for a runtime, or None for a plain shell.

    ``system_prompt_file`` injects role instructions at the system level for
    runtimes that support it (Claude Code's ``--append-system-prompt-file``);
    other runtimes ignore it and callers fall back to message injection.

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
    if system_prompt_file is not None and runtime == "claude":
        command.extend(["--append-system-prompt-file", str(system_prompt_file)])
    command.extend(hook_launch_args(runtime, data_dir, state_dir))
    return command


def launch_environment(
    name: str,
    runtime: str,
    state_dir: Path | str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Environment exported into an agent session so shipped hooks can find the backbone."""
    env = {RUNTIME_ENV_KEY: runtime, AGENT_ENV_KEY: name}
    if state_dir:
        env[STATE_DIR_ENV_KEY] = str(state_dir)
    env.update(extra or {})
    return env


async def start_agent(
    spec: AgentSpec,
    *,
    runtime: str | None = None,
    model: str | None = None,
    resume: bool = False,
    state_dir: Path | str | None = None,
    data_dir: Path | str | None = None,
    pre_trust: bool = False,
    system_prompt_file: Path | str | None = None,
    inject_brief: bool = False,
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
    if pre_trust and effective_runtime == "claude":
        pre_trust_directory(spec.path)
    if system_prompt_file is None and inject_brief and data_dir is not None:
        system_prompt_file = agent_brief_file(
            spec.name, spec.repo, data_dir, runtime=effective_runtime
        )
    try:
        command = build_command(
            effective_runtime,
            model=effective_model,
            resume=resume,
            data_dir=data_dir,
            state_dir=state_dir,
            system_prompt_file=system_prompt_file,
        )
    except (ValueError, RuntimeError) as exc:
        log.error("Cannot start agent '%s': %s", spec.name, exc)
        return False

    environment = launch_environment(spec.name, effective_runtime, state_dir, spec.env)
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


async def wait_until_ready(
    name: str,
    *,
    state_dir: Path | str,
    runtime: str,
    timeout: float = 60.0,
    poll_interval: float = 0.5,
) -> tuple[str, list[str]]:
    """Wait until the agent is at its prompt.

    Returns ``(outcome, evidence)`` with outcome ``ready``, ``waiting_for_human``
    (the runtime is asking something, e.g. a folder-trust prompt), ``exited``
    or ``timeout``. Readiness is a fresh hook-written ``idle`` state, or — for
    runtimes without hooks — a visible empty prompt in the terminal.
    """
    from agent_backbone.services.agents._file_reader import read_state_file
    from agent_backbone.services.agents.models import AgentState

    started = time.monotonic()
    adapter = get_terminal_adapter(runtime)
    state_path = Path(state_dir).expanduser()
    last_pane = ""
    while True:
        if not await session_exists(name):
            return "exited", ["tmux session ended before the agent reached its prompt"]

        snapshot = read_state_file(state_path, name)
        if snapshot and snapshot.timestamp >= time.time() - (time.monotonic() - started) - 2:
            if snapshot.state == AgentState.IDLE:
                return "ready", [f"hook reported idle {time.time() - snapshot.timestamp:.0f}s ago"]
            if snapshot.state == AgentState.WAITING_FOR_HUMAN:
                return "waiting_for_human", [f"hook reported waiting_for_human ({snapshot.reason})"]

        pane = await capture_pane(name, lines=60)
        if pane:
            last_pane = pane
            if adapter.detect_waiting_for_human(pane):
                tail = [ln for ln in sanitize_pane_content(pane).splitlines() if ln.strip()][-6:]
                return "waiting_for_human", ["terminal shows a question for the human:", *tail]
            if adapter.detect_idle(pane):
                return "ready", ["terminal shows an empty prompt"]

        if time.monotonic() - started >= timeout:
            tail = [ln for ln in last_pane.strip().splitlines() if ln.strip()][-3:]
            return "timeout", [f"no prompt after {timeout:.0f}s; last lines: {tail}"]
        await asyncio.sleep(poll_interval)


async def stop_agent(name: str) -> bool:
    """Stop a single agent session."""
    ok = await stop_session(name)
    if ok:
        log.info("Agent '%s' stopped", name)
    return ok
