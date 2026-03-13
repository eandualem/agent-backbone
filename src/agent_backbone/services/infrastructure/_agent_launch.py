"""Shared launch planning for infrastructure-managed agent sessions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_backbone.services.infrastructure._commands import (
    build_agent_command,
    runtime_environment,
)
from agent_backbone.services.terminal import resolve_agent_dir

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig


@dataclass(frozen=True, slots=True)
class AgentLaunchPlan:
    """Resolved launch inputs for an agent tmux session."""

    name: str
    working_dir: str
    command: list[str]
    environment: dict[str, str]


def build_agent_launch_plan(
    name: str,
    config: BackboneConfig,
    *,
    cli: str = "claude",
    model: str | None = None,
) -> AgentLaunchPlan | None:
    """Resolve a complete launch plan for an agent session."""
    working_dir = resolve_agent_dir(name, config.registry)
    if not working_dir or not Path(working_dir).is_dir():
        return None

    return AgentLaunchPlan(
        name=name,
        working_dir=working_dir,
        command=build_agent_command(cli, model),
        environment=runtime_environment(cli),
    )
