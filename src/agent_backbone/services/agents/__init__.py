"""Agents service - agent state tracking + monitoring coordination."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS: dict[str, tuple[str, str]] = {
    "AgentState": ("agent_backbone.services.agents.models", "AgentState"),
    "AgentStateError": ("agent_backbone.services.agents.exceptions", "AgentStateError"),
    "MonitoringError": ("agent_backbone.services.agents.exceptions", "MonitoringError"),
    "MonitoringService": ("agent_backbone.services.agents.interface", "MonitoringService"),
    "StateService": ("agent_backbone.services.agents.interface", "StateService"),
    "StateSnapshot": ("agent_backbone.services.agents.models", "StateSnapshot"),
    "SessionObservation": ("agent_backbone.services.agents._observation", "SessionObservation"),
    "observe_session": ("agent_backbone.services.agents._observation", "observe_session"),
    "snapshot_from_observation": (
        "agent_backbone.services.agents._observation",
        "snapshot_from_observation",
    ),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    """Lazily load agent exports to avoid package import cycles."""
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
