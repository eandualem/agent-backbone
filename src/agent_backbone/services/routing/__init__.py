"""Routing — who hears about an event, and the one safe way text reaches an agent.

The package decides audiences (``_targets``), resolves them to sessions
(``_resolution``), reads delivery readiness (``_intelligence``), formats the
envelope (``_format``) and delivers through ``safe_deliver`` (``_delivery``).
GitHub events enter through ``dispatch_event`` (``_ingest``); subscribed
source events through ``dispatch_source_events`` (``_subscriptions``). The names
below are the surface the API, the jobs and the integrations use.
"""

from agent_backbone.services.routing._create_notify import create_and_notify
from agent_backbone.services.routing._delivery import (
    DeliveryReport,
    checkpoint_inbox,
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
from agent_backbone.services.routing._outbox import retry_outbox
from agent_backbone.services.routing._priority import compute_priority_score
from agent_backbone.services.routing._resolution import validate_issue_targets
from agent_backbone.services.routing._steer import (
    STEER_TTL_SECONDS,
    SteerReport,
    settle_steers,
    steer_agent,
)
from agent_backbone.services.routing._subscriptions import dispatch_source_events
from agent_backbone.services.routing._targets import (
    list_open_queue_for_target,
    queue_scope,
    route_issue,
)
from agent_backbone.services.routing._validity import current_notification_issue

__all__ = [
    "STEER_TTL_SECONDS",
    "DeliveryReport",
    "IssueClosedHook",
    "SteerReport",
    "checkpoint_inbox",
    "compute_priority_score",
    "create_and_notify",
    "current_notification_issue",
    "dispatch_event",
    "dispatch_source_events",
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
    "retry_outbox",
    "route_issue",
    "routing_in_flight",
    "safe_deliver",
    "settle_steers",
    "stamp_queued_age",
    "steer_agent",
    "sync_dependencies",
    "validate_issue_targets",
]
