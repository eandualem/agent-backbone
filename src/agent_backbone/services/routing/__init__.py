"""Routing service — dispatch, delivery, and notification formatting."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS: dict[str, tuple[str, str]] = {
    # Dispatch
    "DispatchResult": ("agent_backbone.services.routing._router", "DispatchResult"),
    "DispatchResultModel": ("agent_backbone.services.routing.models", "DispatchResult"),
    "DispatchService": ("agent_backbone.services.routing.interface", "DispatchService"),
    "_ONBOARDING_TITLE_PREFIX": (
        "agent_backbone.services.routing._lifecycle",
        "_ONBOARDING_TITLE_PREFIX",
    ),
    "_check_onboarding_chain": (
        "agent_backbone.services.routing._lifecycle",
        "_check_onboarding_chain",
    ),
    "check_parent_resolved": (
        "agent_backbone.services.routing._dependencies",
        "check_parent_resolved",
    ),
    "deliver_next": ("agent_backbone.services.routing._lifecycle", "deliver_next"),
    "find_next_issue": ("agent_backbone.services.routing._lifecycle", "find_next_issue"),
    "issue_dispatcher": ("agent_backbone.services.routing._router", "issue_dispatcher"),
    "on_dependency_resolved": (
        "agent_backbone.services.routing._dependencies",
        "on_dependency_resolved",
    ),
    "on_issue_closed": ("agent_backbone.services.routing._lifecycle", "on_issue_closed"),
    "resolve_session": ("agent_backbone.services.routing._router", "resolve_session"),
    "sync_dependencies": ("agent_backbone.services.routing._dependencies", "sync_dependencies"),
    # Delivery
    "DEFAULT_DEDUP_SECONDS": ("agent_backbone.services.routing._dedup", "DEFAULT_DEDUP_SECONDS"),
    "DeliveryService": ("agent_backbone.services.routing.interface", "DeliveryService"),
    "DeliveryServiceError": (
        "agent_backbone.services.routing.exceptions",
        "DeliveryServiceError",
    ),
    "SessionIntelligence": ("agent_backbone.services.routing.models", "SessionIntelligence"),
    "SessionProfile": ("agent_backbone.services.routing.models", "SessionProfile"),
    "clear": ("agent_backbone.services.routing._dedup", "clear"),
    "compute_priority_score": (
        "agent_backbone.services.routing._priority",
        "compute_priority_score",
    ),
    "create_and_notify": ("agent_backbone.services.routing._create_notify", "create_and_notify"),
    "delivery_retry": ("agent_backbone.services.routing._flows", "delivery_retry"),
    "get_session_intelligence": (
        "agent_backbone.services.routing._intelligence",
        "get_session_intelligence",
    ),
    "is_http_target": ("agent_backbone.services.routing._intelligence", "is_http_target"),
    "is_recent_notification": (
        "agent_backbone.services.routing._dedup",
        "is_recent_notification",
    ),
    "list_sessions_full": ("agent_backbone.services.routing._delivery", "list_sessions_full"),
    "resolve_entity_session": (
        "agent_backbone.services.routing._resolution",
        "resolve_entity_session",
    ),
    "resolve_entity_sessions": (
        "agent_backbone.services.routing._resolution",
        "resolve_entity_sessions",
    ),
    "retry_delivery": ("agent_backbone.services.routing._flows", "retry_delivery"),
    "safe_deliver": ("agent_backbone.services.routing._delivery", "safe_deliver"),
    "scheduled_delivery": ("agent_backbone.services.routing._flows", "scheduled_delivery"),
    # Notifications
    "NotificationError": ("agent_backbone.services.routing.exceptions", "NotificationError"),
    "NotificationService": ("agent_backbone.services.routing._format", "NotificationService"),
    "format_comment_notification": (
        "agent_backbone.services.routing._format",
        "format_comment_notification",
    ),
    "format_digest": ("agent_backbone.services.routing._format", "format_digest"),
    "format_issue_notification": (
        "agent_backbone.services.routing._format",
        "format_issue_notification",
    ),
    "format_next_issue_notification": (
        "agent_backbone.services.routing._format",
        "format_next_issue_notification",
    ),
    "format_plan_notification": (
        "agent_backbone.services.routing._format",
        "format_plan_notification",
    ),
    "format_stall_notification": (
        "agent_backbone.services.routing._format",
        "format_stall_notification",
    ),
    "format_unblock_notification": (
        "agent_backbone.services.routing._format",
        "format_unblock_notification",
    ),
    "format_unexpected_offline_notification": (
        "agent_backbone.services.routing._format",
        "format_unexpected_offline_notification",
    ),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    """Lazily load routing exports to reduce package import coupling."""
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
