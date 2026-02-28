"""Delivery service — session resolution, intelligence, and safe delivery."""

from agent_backbone.services.delivery._dedup import (
    DEFAULT_DEDUP_SECONDS,
    clear,
    is_recent_notification,
)
from agent_backbone.services.delivery._delivery import (
    list_sessions_full,
    safe_deliver,
)
from agent_backbone.services.delivery._flows import (
    delivery_retry,
    retry_delivery,
    scheduled_delivery,
)
from agent_backbone.services.delivery._intelligence import (
    get_session_intelligence,
    is_http_target,
)
from agent_backbone.services.delivery._priority import compute_priority_score
from agent_backbone.services.delivery._resolution import resolve_entity_session
from agent_backbone.services.delivery.exceptions import DeliveryServiceError
from agent_backbone.services.delivery.interface import DeliveryService
from agent_backbone.services.delivery.models import SessionIntelligence, SessionProfile

__all__ = [
    "DEFAULT_DEDUP_SECONDS",
    "DeliveryService",
    "DeliveryServiceError",
    "SessionIntelligence",
    "SessionProfile",
    "clear",
    "compute_priority_score",
    "delivery_retry",
    "get_session_intelligence",
    "is_http_target",
    "is_recent_notification",
    "list_sessions_full",
    "resolve_entity_session",
    "retry_delivery",
    "safe_deliver",
    "scheduled_delivery",
]
