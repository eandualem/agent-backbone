"""Status collection and formatting across all infrastructure."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agent_backbone.services.infrastructure._processes import pid_for_port, read_pid
from agent_backbone.services.terminal import list_sessions, session_exists

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig

log = logging.getLogger(__name__)


async def show_status(config: BackboneConfig) -> str:
    """Collect and format status of all infrastructure services and agents."""
    lines: list[str] = []

    # --- Services ---
    lines.append("=== Services ===")

    # Gateway — check both tmux session and port
    gateway_port = config.gateway.port
    gateway_pid = await pid_for_port(gateway_port)
    gateway_session = await session_exists("gateway")
    if gateway_pid:
        ctx = "tmux session" if gateway_session else "outside tmux"
        lines.append(f"  gateway    : running ({ctx}, port {gateway_port}, pid {gateway_pid})")
    elif gateway_session:
        lines.append(f"  gateway    : starting (tmux session, port {gateway_port} not yet bound)")
    else:
        lines.append("  gateway    : not running")

    # Telegram
    telegram_pid = read_pid("telegram")
    telegram_running = await session_exists("telegram-bot")
    tpid_info = f", pid {telegram_pid}" if telegram_pid else ""
    if telegram_running:
        lines.append(f"  telegram   : running (tmux session, long-polling{tpid_info})")
    elif telegram_pid:
        lines.append(f"  telegram   : running (outside tmux{tpid_info})")
    else:
        lines.append("  telegram   : not running")

    # --- Named Entities ---
    lines.append("")
    lines.append("=== Named Entities ===")
    for name in sorted(config.registry.entities.keys()):
        running = await session_exists(name)
        status = "running" if running else "not running"
        lines.append(f"  {name:<20s}: {status}")

    # --- Coding Agents ---
    sessions = set(await list_sessions())
    coding_total = len(config.registry.repos)
    coding_running = sum(1 for r in config.registry.repos if r.name in sessions)
    lines.append("")
    lines.append(f"=== Coding Agents ({coding_running}/{coding_total} running) ===")
    running_repos = [r.name for r in config.registry.repos if r.name in sessions]
    if running_repos:
        for name in sorted(running_repos):
            lines.append(f"  {name:<30s}: running")
    else:
        lines.append("  (none running)")

    # --- Unrecognized Sessions ---
    service_sessions = config.entities.service_sessions
    known = (
        service_sessions
        | set(config.registry.entity_by_session.keys())
        | config.registry.repo_names
    )
    unknown = sorted(s for s in sessions if s not in known)
    if unknown:
        lines.append("")
        lines.append("=== Unrecognized Sessions ===")
        for s in unknown:
            lines.append(f"  {s}")

    return "\n".join(lines)
