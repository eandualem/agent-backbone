"""Routing service — dispatch, delivery, and notification formatting.

Unified package combining event routing (dispatch), message delivery,
session intelligence, and notification formatting.
"""

from agent_backbone.services.routing._create_notify import create_and_notify
from agent_backbone.services.routing._dedup import (
    DEFAULT_DEDUP_SECONDS,
    clear,
    is_recent_notification,
)
from agent_backbone.services.routing._delivery import safe_deliver
from agent_backbone.services.routing._dependencies import (
    check_parent_resolved,
    on_dependency_resolved,
    sync_dependencies,
)
from agent_backbone.services.routing._flows import (
    delivery_retry,
    drain_message_queue,
    retry_delivery,
)
from agent_backbone.services.routing._format import (
    format_closed_notification,
    format_comment_notification,
    format_issue_notification,
    format_next_issue_notification,
    format_plan_notification,
    format_pull_request_notification,
    format_stall_notification,
    format_unassigned_notification,
    format_unblock_notification,
    format_unexpected_offline_notification,
    format_watch_notification,
)
from agent_backbone.services.routing._intelligence import get_session_intelligence
from agent_backbone.services.routing._lifecycle import (
    deliver_next,
    find_next_issue,
    on_issue_closed,
)
from agent_backbone.services.routing._priority import compute_priority_score
from agent_backbone.services.routing._resolution import (
    is_valid_issue_target,
    resolve_entity_session,
    validate_issue_targets,
)
from agent_backbone.services.routing._router import issue_dispatcher
from agent_backbone.services.routing._targets import (
    EventRouting,
    comment_audience,
    list_open_queue_for_target,
    queue_scope,
    route_issue_event,
)
from agent_backbone.services.routing.interface import DeliveryService, DispatchService
from agent_backbone.services.routing.models import (
    DispatchResult,
    SessionIntelligence,
    SessionProfile,
)

__all__ = [
    "DEFAULT_DEDUP_SECONDS",
    "DeliveryService",
    "DispatchResult",
    "DispatchService",
    "EventRouting",
    "SessionIntelligence",
    "SessionProfile",
    "check_parent_resolved",
    "clear",
    "comment_audience",
    "compute_priority_score",
    "create_and_notify",
    "deliver_next",
    "delivery_retry",
    "drain_message_queue",
    "find_next_issue",
    "format_closed_notification",
    "format_comment_notification",
    "format_issue_notification",
    "format_next_issue_notification",
    "format_plan_notification",
    "format_pull_request_notification",
    "format_stall_notification",
    "format_unassigned_notification",
    "format_unblock_notification",
    "format_unexpected_offline_notification",
    "format_watch_notification",
    "get_session_intelligence",
    "is_recent_notification",
    "is_valid_issue_target",
    "issue_dispatcher",
    "list_open_queue_for_target",
    "on_dependency_resolved",
    "on_issue_closed",
    "queue_scope",
    "resolve_entity_session",
    "retry_delivery",
    "route_issue_event",
    "safe_deliver",
    "sync_dependencies",
    "validate_issue_targets",
]
