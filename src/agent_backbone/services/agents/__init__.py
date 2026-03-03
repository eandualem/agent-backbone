"""Agents service — agent state tracking + monitoring coordination."""

from agent_backbone.services.agents._delivery_check import (
    find_outgoing_comment,
    has_commented_on_issue,
    should_deliver,
)
from agent_backbone.services.agents._escalation import (
    _escalation_dedup,
    _plan_notify_dedup,
    _should_escalate,
    check_for_stalls,
    check_for_unexpected_offline,
    check_plan_waiting,
    handle_offline,
    handle_stalls,
)
from agent_backbone.services.agents._file_reader import read_state_file
from agent_backbone.services.agents._heartbeat import (
    _heartbeat_lock,
    evaluate_agent_heartbeat,
    heartbeat_scheduler,
    is_due,
    load_schedules,
    save_schedules,
)
from agent_backbone.services.agents._inference import get_agent_state, infer_state_from_pane
from agent_backbone.services.agents._monitor import monitor_agents
from agent_backbone.services.agents._pending import (
    check_pending_issues,
    deliver_pending_issues,
)
from agent_backbone.services.agents.exceptions import AgentStateError, MonitoringError
from agent_backbone.services.agents.interface import MonitoringService, StateService
from agent_backbone.services.agents.models import AgentState, StateSnapshot

__all__ = [
    # State
    "AgentState",
    "AgentStateError",
    "StateService",
    "StateSnapshot",
    "find_outgoing_comment",
    "get_agent_state",
    "has_commented_on_issue",
    "infer_state_from_pane",
    "read_state_file",
    "should_deliver",
    # Monitoring
    "MonitoringError",
    "MonitoringService",
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
