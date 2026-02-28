"""Monitoring service — agent monitor, escalation, pending delivery, heartbeat."""

from agent_backbone.services.monitoring._escalation import (
    _escalation_dedup,
    _plan_notify_dedup,
    _should_escalate,
    check_for_stalls,
    check_for_unexpected_offline,
    check_plan_waiting,
    handle_offline,
    handle_stalls,
)
from agent_backbone.services.monitoring._heartbeat import (
    _heartbeat_lock,
    evaluate_agent_heartbeat,
    heartbeat_scheduler,
    is_due,
    load_schedules,
    save_schedules,
)
from agent_backbone.services.monitoring._monitor import monitor_agents
from agent_backbone.services.monitoring._pending import (
    check_pending_issues,
    deliver_pending_issues,
)

__all__ = [
    "_escalation_dedup",
    "_heartbeat_lock",
    "_plan_notify_dedup",
    "_should_escalate",
    "check_for_stalls",
    "check_for_unexpected_offline",
    "check_pending_issues",
    "check_plan_waiting",
    "deliver_pending_issues",
    "evaluate_agent_heartbeat",
    "handle_offline",
    "handle_stalls",
    "heartbeat_scheduler",
    "is_due",
    "load_schedules",
    "monitor_agents",
    "save_schedules",
]
