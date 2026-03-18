"""Agent group operations — start/stop agents, orchestrators, org groups."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from agent_backbone.services.terminal import (
    RUNTIME_ENV_KEY,
    list_sessions,
    resolve_agent_dir,
    session_exists,
    start_session,
    stop_session,
)

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig

log = logging.getLogger(__name__)


async def start_agent(
    name: str,
    config: BackboneConfig,
    cli: str = "claude",
    model: str | None = None,
) -> bool:
    """Start a single agent session.

    Resolves the working directory from the registry. Supports cli and model overrides.
    """
    if await session_exists(name):
        log.info("Agent '%s' already running", name)
        return True

    working_dir = resolve_agent_dir(name, config.registry)
    if not working_dir:
        log.error("Unknown agent '%s' — not in registry", name)
        return False
    if not Path(working_dir).is_dir():
        log.error("Directory '%s' does not exist for agent '%s'", working_dir, name)
        return False

    command = [cli]
    if model:
        command.extend(["--model", model])

    ok = await start_session(
        name,
        working_dir=working_dir,
        command=command,
        environment={RUNTIME_ENV_KEY: cli},
    )
    if ok:
        extra = f", model: {model}" if model else ""
        log.info("Agent '%s' started (cli: %s%s)", name, cli, extra)
    return ok


async def stop_agent(name: str) -> bool:
    """Stop a single agent session."""
    ok = await stop_session(name)
    if ok:
        log.info("Agent '%s' stopped", name)
    return ok


async def start_group(
    label: str,
    names: list[str],
    config: BackboneConfig,
    cli: str = "claude",
    model: str | None = None,
) -> int:
    """Start a group of agents. Returns count of newly started agents."""
    started = 0
    log.info("=== Starting %s ===", label)
    for name in names:
        if await session_exists(name):
            log.info("  Already running: %s", name)
            continue

        working_dir = resolve_agent_dir(name, config.registry)
        if not working_dir or not Path(working_dir).is_dir():
            log.warning("  Skipped (dir not found): %s", name)
            continue

        command = [cli]
        if model:
            command.extend(["--model", model])

        ok = await start_session(
            name,
            working_dir=working_dir,
            command=command,
            environment={RUNTIME_ENV_KEY: cli},
        )
        if ok:
            extra = f", model: {model}" if model else ""
            log.info("  Started: %s (cli: %s%s)", name, cli, extra)
            started += 1

    log.info("Started %d agent(s)", started)
    return started


async def stop_all_agents(config: BackboneConfig) -> int:
    """Stop all agent sessions (excluding service sessions)."""
    sessions = await list_sessions()
    service_sessions = config.entities.service_sessions
    stopped = 0
    for s in sessions:
        if s in service_sessions:
            continue
        ok = await stop_session(s)
        if ok:
            log.info("  Stopped: %s", s)
            stopped += 1
    if stopped == 0:
        log.info("No agent sessions running")
    else:
        log.info("Stopped %d agent session(s)", stopped)
    return stopped


async def start_orchestrators(
    config: BackboneConfig,
    cli: str = "claude",
    model: str | None = None,
) -> int:
    """Start all entities in the 'orchestrators' group."""
    names = [
        name for name, entry in config.registry.entities.items() if "orchestrators" in entry.groups
    ]
    if not names:
        log.warning("No orchestrators found in registry")
        return 0
    return await start_group("Orchestrators", names, config, cli=cli, model=model)


async def start_org(
    org: str,
    config: BackboneConfig,
    cli: str = "claude",
    model: str | None = None,
) -> int:
    """Start all coding agents for an organization."""
    names = [r.name for r in config.registry.repos if r.org == org]
    if not names:
        log.warning("No repos found for org '%s'", org)
        return 0
    return await start_group(org, names, config, cli=cli, model=model)


async def start_all(
    config: BackboneConfig,
    cli: str = "claude",
    model: str | None = None,
) -> int:
    """Start all orchestrators + standalone agents + all coding agents."""
    total = 0
    total += await start_orchestrators(config, cli=cli, model=model)

    # Standalone agents
    standalone = [
        name for name, entry in config.registry.entities.items() if "standalone" in entry.groups
    ]
    if standalone:
        total += await start_group("Standalone Agents", standalone, config, cli=cli, model=model)

    # All orgs
    for org in sorted(config.registry.orgs):
        total += await start_org(org, config, cli=cli, model=model)

    return total


def list_agents(config: BackboneConfig) -> str:
    """Format a list of all known agents with their directories."""
    lines = ["=== Named Entities ==="]
    for name, entry in config.registry.entities.items():
        home = str(Path(entry.home).expanduser())
        lines.append(f"  {name:<20s} {home}")

    # Group repos by org
    orgs: dict[str, list[str]] = {}
    for repo in config.registry.repos:
        orgs.setdefault(repo.org, []).append(f"  {repo.name:<30s} {repo.path}")

    for org in sorted(orgs):
        lines.append(f"\n=== Coding Agents ({org}) ===")
        lines.extend(orgs[org])

    return "\n".join(lines)
