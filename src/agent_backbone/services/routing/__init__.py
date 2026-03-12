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
from agent_backbone.services.routing._delivery import (
    list_sessions_full,
    safe_deliver,
)
from agent_backbone.services.routing._dependencies import (
    check_parent_resolved,
    on_dependency_resolved,
    sync_dependencies,
)
from agent_backbone.services.routing._flows import (
    delivery_retry,
    retry_delivery,
    scheduled_delivery,
)
from agent_backbone.services.routing._format import (
    NotificationService,
    format_comment_notification,
    format_digest,
    format_issue_notification,
    format_next_issue_notification,
    format_plan_notification,
    format_stall_notification,
    format_unblock_notification,
    format_unexpected_offline_notification,
)
from agent_backbone.services.routing._intelligence import (
    get_session_intelligence,
    is_http_target,
)
from agent_backbone.services.routing._lifecycle import (
    _ONBOARDING_TITLE_PREFIX,
    _check_onboarding_chain,
    deliver_next,
    find_next_issue,
    on_issue_closed,
)
from agent_backbone.services.routing._priority import compute_priority_score
from agent_backbone.services.routing._resolution import resolve_entity_session
from agent_backbone.services.routing._router import (
    DispatchResult,
    issue_dispatcher,
    resolve_session,
)
from agent_backbone.services.routing.exceptions import (
    DeliveryServiceError,
    NotificationError,
)
from agent_backbone.services.routing.interface import DeliveryService, DispatchService
from agent_backbone.services.routing.models import (
    DispatchResult as DispatchResultModel,
)
from agent_backbone.services.routing.models import (
    SessionIntelligence,
    SessionProfile,
)

__all__ = [
    # Dispatch
    "DispatchResult",
    "DispatchResultModel",
    "DispatchService",
    "_ONBOARDING_TITLE_PREFIX",
    "_check_onboarding_chain",
    "check_parent_resolved",
    "deliver_next",
    "find_next_issue",
    "issue_dispatcher",
    "on_dependency_resolved",
    "on_issue_closed",
    "resolve_session",
    "sync_dependencies",
    # Delivery
    "DEFAULT_DEDUP_SECONDS",
    "DeliveryService",
    "DeliveryServiceError",
    "SessionIntelligence",
    "SessionProfile",
    "clear",
    "compute_priority_score",
    "create_and_notify",
    "delivery_retry",
    "get_session_intelligence",
    "is_http_target",
    "is_recent_notification",
    "list_sessions_full",
    "resolve_entity_session",
    "retry_delivery",
    "safe_deliver",
    "scheduled_delivery",
    # Notifications
    "NotificationError",
    "NotificationService",
    "format_comment_notification",
    "format_digest",
    "format_issue_notification",
    "format_next_issue_notification",
    "format_plan_notification",
    "format_stall_notification",
    "format_unexpected_offline_notification",
    "format_unblock_notification",
]
