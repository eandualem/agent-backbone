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
    "find_outgoing_comment": (
        "agent_backbone.services.agents._delivery_check",
        "find_outgoing_comment",
    ),
    "get_agent_state": ("agent_backbone.services.agents._inference", "get_agent_state"),
    "has_commented_on_issue": (
        "agent_backbone.services.agents._delivery_check",
        "has_commented_on_issue",
    ),
    "infer_state_from_pane": (
        "agent_backbone.services.agents._inference",
        "infer_state_from_pane",
    ),
    "read_state_file": ("agent_backbone.services.agents._file_reader", "read_state_file"),
    "should_deliver": ("agent_backbone.services.agents._delivery_check", "should_deliver"),
    "_escalation_dedup": (
        "agent_backbone.services.agents._escalation",
        "_escalation_dedup",
    ),
    "_heartbeat_lock": ("agent_backbone.services.agents._heartbeat", "_heartbeat_lock"),
    "_plan_notify_dedup": (
        "agent_backbone.services.agents._escalation",
        "_plan_notify_dedup",
    ),
    "_should_escalate": ("agent_backbone.services.agents._escalation", "_should_escalate"),
    "check_for_stalls": ("agent_backbone.services.agents._escalation", "check_for_stalls"),
    "check_for_unexpected_offline": (
        "agent_backbone.services.agents._escalation",
        "check_for_unexpected_offline",
    ),
    "check_pending_issues": ("agent_backbone.services.agents._pending", "check_pending_issues"),
    "check_plan_waiting": ("agent_backbone.services.agents._escalation", "check_plan_waiting"),
    "deliver_pending_issues": (
        "agent_backbone.services.agents._pending",
        "deliver_pending_issues",
    ),
    "evaluate_agent_heartbeat": (
        "agent_backbone.services.agents._heartbeat",
        "evaluate_agent_heartbeat",
    ),
    "handle_offline": ("agent_backbone.services.agents._escalation", "handle_offline"),
    "handle_stalls": ("agent_backbone.services.agents._escalation", "handle_stalls"),
    "heartbeat_scheduler": (
        "agent_backbone.services.agents._heartbeat",
        "heartbeat_scheduler",
    ),
    "is_due": ("agent_backbone.services.agents._heartbeat", "is_due"),
    "load_schedules": ("agent_backbone.services.agents._heartbeat", "load_schedules"),
    "monitor_agents": ("agent_backbone.services.agents._monitor", "monitor_agents"),
    "reconcile_startup_states": (
        "agent_backbone.services.agents._reconciliation",
        "reconcile_startup_states",
    ),
    "save_schedules": ("agent_backbone.services.agents._heartbeat", "save_schedules"),
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
