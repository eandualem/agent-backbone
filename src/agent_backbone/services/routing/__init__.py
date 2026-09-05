"""Routing — who hears about an event, and the one safe way text reaches an agent.

The package decides audiences (``_targets``), resolves them to sessions
(``_resolution``), reads delivery readiness (``_intelligence``), formats the
envelope (``_format``) and delivers through ``safe_deliver`` (``_delivery``).
GitHub events enter through ``dispatch_event`` (``_ingest``). The names
below are the surface the API, the jobs and the integrations use.
"""

from agent_backbone.services.routing._create_notify import create_and_notify
from agent_backbone.services.routing._delivery import (
    DeliveryReport,
    deliver,
    is_acknowledged,
    queue_detail,
    safe_deliver,
)
from agent_backbone.services.routing._dependencies import sync_dependencies
from agent_backbone.services.routing._format import (
    format_next_issue_notification,
    format_offline_queue_notification,
    format_plan_notification,
    format_review_notification,
    format_stall_notification,
    format_unexpected_offline_notification,
    stamp_queued_age,
)
from agent_backbone.services.routing._ingest import (
    IssueClosedHook,
    dispatch_event,
    routing_in_flight,
)
from agent_backbone.services.routing._intelligence import get_session_intelligence
from agent_backbone.services.routing._priority import compute_priority_score
from agent_backbone.services.routing._resolution import validate_issue_targets
from agent_backbone.services.routing._targets import (
    list_open_queue_for_target,
    queue_scope,
    route_issue,
)

__all__ = [
    "DeliveryReport",
    "IssueClosedHook",
    "compute_priority_score",
    "create_and_notify",
    "deliver",
    "dispatch_event",
    "format_next_issue_notification",
    "format_offline_queue_notification",
    "format_plan_notification",
    "format_review_notification",
    "format_stall_notification",
    "format_unexpected_offline_notification",
    "get_session_intelligence",
    "is_acknowledged",
    "list_open_queue_for_target",
    "queue_detail",
    "queue_scope",
    "route_issue",
    "routing_in_flight",
    "safe_deliver",
    "stamp_queued_age",
    "sync_dependencies",
    "validate_issue_targets",
]
