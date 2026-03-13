"""Shared delivery-policy helpers for routing flows."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_backbone.services.routing._targets import list_open_queue_for_target

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.routing.models import DispatchResult

BUSY_DELIVERY_OUTCOMES = frozenset(
    {
        "agent_working",
        "plan_waiting",
        "copy_mode",
        "user_interacting",
        "grace_period",
    }
)
PASSTHROUGH_DELIVERY_OUTCOMES = frozenset({"already_delivered", "awaiting_ack", "unknown_state"})


async def load_queue_scope_issue_numbers(
    config: BackboneConfig,
    target: str,
    gh: object,
    *,
    issue_repo_full_name: str = "",
) -> set[int]:
    """Load the current open issue numbers that define a target's queue scope."""
    issues = await list_open_queue_for_target(
        config,
        target,
        gh,
        issue_repo_full_name=issue_repo_full_name,
    )
    return {issue.number for issue in issues}


def record_dispatch_outcome(result: DispatchResult, session_name: str, outcome: str) -> None:
    """Map a ``safe_deliver`` outcome onto the public ``DispatchResult`` buckets."""
    if outcome == "delivered":
        result.delivered.append(session_name)
        return
    if outcome == "already_delivered":
        result.skipped.append(session_name)
        return
    if outcome in {"offline", "delivery_failed"}:
        result.offline.append(session_name)
        return
    result.deferred.append(session_name)


def normalize_retry_delivery_outcome(outcome: str) -> str:
    """Translate ``safe_deliver`` outcomes for the retry flow contract."""
    if outcome == "delivered":
        return "retried"
    if outcome == "offline":
        return "still_offline"
    if outcome in BUSY_DELIVERY_OUTCOMES:
        return "still_busy"
    if outcome in PASSTHROUGH_DELIVERY_OUTCOMES:
        return outcome
    return "delivery_failed"


def normalize_scheduled_delivery_outcome(outcome: str) -> str:
    """Translate ``safe_deliver`` outcomes for the scheduled-delivery contract."""
    if outcome == "delivered":
        return "delivered"
    if outcome == "offline":
        return "offline"
    if outcome in BUSY_DELIVERY_OUTCOMES:
        return "busy"
    if outcome in PASSTHROUGH_DELIVERY_OUTCOMES:
        return outcome
    return "delivery_failed"
