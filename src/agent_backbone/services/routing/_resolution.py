"""Target-to-session resolution.

An agent's name is its tmux session name, so resolution is mostly a lookup
against the configured agents plus the routing ignore list.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig

log = logging.getLogger(__name__)


def is_valid_issue_target(target: str, config: BackboneConfig) -> bool:
    """Whether a ``for:`` issue target is routable (configured agent or ignored name)."""
    return target in config.routing.ignore_targets or target in config.agents


def validate_issue_targets(targets: list[str], config: BackboneConfig) -> None:
    """Reject issue targets that are not configured agents."""
    invalid = [target for target in targets if not is_valid_issue_target(target, config)]
    if invalid:
        known = ", ".join(sorted(config.agents.names)) or "<none configured>"
        raise ValueError(
            f"unknown issue target(s): {', '.join(invalid)}; configured agents: {known}"
        )


def resolve_entity_session(target: str, config: BackboneConfig) -> str | None:
    """Resolve a target name to a tmux session name (or None if not routable)."""
    if target in config.routing.ignore_targets:
        return None
    if target in config.agents:
        return target
    return None


def resolve_entity_sessions(target: str, config: BackboneConfig) -> list[str]:
    """Resolve a target to its delivery sessions (zero or one)."""
    session = resolve_entity_session(target, config)
    return [session] if session else []
