"""Safe delivery — state-aware message delivery with optional queuing."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.database import BackboneDB

from agent_backbone.services.routing._intelligence import get_session_intelligence, is_http_target
from agent_backbone.services.routing.models import SessionIntelligence, SessionProfile
from agent_backbone.services.terminal import get_terminal_adapter, send_message

log = logging.getLogger(__name__)

_SUCCESSFUL_OUTCOME_SUFFIXES = ("delivered", "retried")
_COPY_MODE_RECHECK_DELAY_SECONDS = 0.1


def _comment_matches_active_issue(issue_number: int | None, current_issue: int | None) -> bool:
    """Whether a comment belongs to the issue the agent is actively working on."""
    return issue_number is not None and current_issue is not None and issue_number == current_issue


def _can_track_issue_delivery(
    db: BackboneDB | None,
    issue_number: int | None,
    target_entity: str | None,
) -> bool:
    """Whether this delivery has enough metadata for queue coordination."""
    return db is not None and issue_number is not None and target_entity is not None


async def _is_acknowledged_for_session(
    db: BackboneDB,
    issue_number: int,
    target_entity: str,
    session_name: str,
) -> bool:
    """Check acknowledgment using both entity and session fallback keys."""
    if await db.is_acknowledged(issue_number, target_entity):
        return True
    if session_name != target_entity and await db.is_acknowledged(issue_number, session_name):
        return True
    return False


async def _has_successful_issue_delivery(
    db: BackboneDB,
    issue_number: int,
    session_name: str,
) -> bool:
    """Whether this issue was already successfully delivered to this session."""
    rows = await db.query_deliveries(
        issue_number=issue_number,
        session_name=session_name,
        limit=25,
    )
    return any((row.get("outcome") or "").endswith(_SUCCESSFUL_OUTCOME_SUFFIXES) for row in rows)


async def _get_unacknowledged_gate_issue(
    db: BackboneDB,
    session_name: str,
    current_issue: int,
    queue_scope_issue_numbers: Collection[int] | None = None,
) -> int | None:
    """Return the most recent successful issue still awaiting acknowledgment."""
    scoped_issue_numbers = set(queue_scope_issue_numbers or ())
    rows = await db.query_deliveries(session_name=session_name, limit=100)
    for row in rows:
        issue_number = row.get("issue_number")
        target_entity = row.get("target_entity")
        if not isinstance(issue_number, int) or not isinstance(target_entity, str):
            continue
        if scoped_issue_numbers and issue_number not in scoped_issue_numbers:
            continue
        if issue_number == current_issue:
            continue
        if not (row.get("outcome") or "").endswith(_SUCCESSFUL_OUTCOME_SUFFIXES):
            continue
        if await _is_acknowledged_for_session(db, issue_number, target_entity, session_name):
            continue
        return issue_number
    return None


async def _record_issue_attempt(
    db: BackboneDB | None,
    issue_number: int | None,
    target_entity: str | None,
    session_name: str,
    outcome: str,
    flow_name: str,
    delivery_kind: str = "issue",
) -> None:
    """Persist a queue delivery attempt when tracking metadata is available."""
    if not _can_track_issue_delivery(db, issue_number, target_entity):
        return
    recorded_outcome = outcome
    if delivery_kind != "issue":
        recorded_outcome = f"{delivery_kind}_{outcome}"
    try:
        await db.record_delivery(
            issue_number=issue_number,
            target_entity=target_entity,
            session_name=session_name,
            outcome=recorded_outcome,
            flow_name=flow_name,
        )
    except Exception:
        log.exception("Failed to record delivery attempt (non-fatal)")


async def _persist_delivery_outcome(
    db: BackboneDB | None,
    issue_number: int | None,
    target_entity: str | None,
    session_name: str,
    outcome: str,
    flow_name: str,
    delivery_kind: str,
    delivery_claim_id: int | None,
) -> None:
    """Finalize a claimed issue delivery or fall back to standard attempt logging."""
    if delivery_claim_id is not None and db is not None:
        try:
            await db.finalize_delivery_attempt(delivery_claim_id, outcome)
        except Exception:
            log.exception("Failed to finalize delivery attempt (non-fatal)")
        return
    await _record_issue_attempt(
        db,
        issue_number,
        target_entity,
        session_name,
        outcome,
        flow_name,
        delivery_kind=delivery_kind,
    )


async def _clear_matching_queue_rows(
    db: BackboneDB | None,
    session_name: str,
    message: str,
    delivery_kind: str,
    issue_number: int | None,
) -> None:
    """Clear stale queued duplicates once a non-issue notification succeeds."""
    if db is None or delivery_kind not in {"comment", "direct_message"}:
        return
    try:
        cleared = await db.mark_matching_messages_delivered(
            session_name=session_name,
            message=message,
            delivery_kind=delivery_kind,
            issue_number=issue_number,
        )
        if cleared:
            log.info(
                "Cleared %d stale queued %s notification(s) for %s",
                cleared,
                delivery_kind,
                session_name,
            )
    except Exception:
        log.exception("Failed to clear matching queued notifications (non-fatal)")


async def _recover_copy_mode(
    session_name: str,
    config: BackboneConfig,
    profile: SessionProfile,
    idle_since: float | None = None,
) -> SessionProfile:
    """Attempt to clear copy mode and refresh session intelligence."""
    adapter = get_terminal_adapter(profile.runtime)
    if not await adapter.exit_copy_mode(session_name):
        return profile

    await asyncio.sleep(_COPY_MODE_RECHECK_DELAY_SECONDS)
    refreshed = await get_session_intelligence(session_name, config, idle_since=idle_since)
    log.info(
        "Copy mode recovery for '%s': %s -> %s",
        session_name,
        profile.intelligence,
        refreshed.intelligence,
    )
    return refreshed


async def safe_deliver(
    session_name: str,
    message: str,
    config: BackboneConfig,
    *,
    db: BackboneDB | None = None,
    issue_number: int | None = None,
    target_entity: str | None = None,
    flow_name: str = "",
    priority: bool = False,
    idle_since: float | None = None,
    enforce_issue_queue: bool = False,
    queue_scope_issue_numbers: Collection[int] | None = None,
    delivery_kind: str = "issue",
) -> str:
    """Deliver a message with composite state pre-checks and optional queuing.

    Checks session intelligence before delivering. Non-deliverable states
    enqueue to SQLite when issue_number + target_entity are provided.

    Args:
        session_name: target tmux session.
        message: notification text to deliver.
        config: backbone configuration.
        issue_number: issue number for queue tracking (optional).
        target_entity: entity name for queue tracking (optional).
        flow_name: originating flow for logging/tracking.
        priority: if True, bypass COPY_MODE and USER_INTERACTING checks.
        idle_since: monotonic timestamp when the session became idle.
        enforce_issue_queue: if True, apply session-level dedup and
            acknowledgment gating before attempting delivery.
        queue_scope_issue_numbers: current open queue for the target entity.
            When provided, only deliveries for issues still in that queue can
            block subsequent issue delivery.
        delivery_kind: delivery classification for persistence. Non-issue
            kinds are recorded with a prefixed outcome (for example
            ``comment_delivered``) so they do not interfere with issue
            assignment gating.

    Returns:
        Outcome string: "delivered", "offline", "user_interacting", "agent_working",
        "plan_waiting", "permission_waiting", "grace_period",
        "already_delivered", "awaiting_ack", "delivery_failed".
    """
    if _can_track_issue_delivery(db, issue_number, target_entity):
        # Issue-level dedup: only for issue deliveries. Comments are distinct
        # per webhook delivery_id — a prior comment_delivered on the same issue
        # must NOT block future comments on that issue.
        if delivery_kind == "issue":
            if await _has_successful_issue_delivery(db, issue_number, session_name):
                log.info(
                    "Suppressed duplicate issue delivery for #%d -> %s",
                    issue_number,
                    session_name,
                )
                return "already_delivered"

        # Issue queue gating: only for issue deliveries with enforcement
        if delivery_kind == "issue" and enforce_issue_queue:
            blocking_issue = await _get_unacknowledged_gate_issue(
                db,
                session_name,
                issue_number,
                queue_scope_issue_numbers=queue_scope_issue_numbers,
            )
            if blocking_issue is not None:
                log.info(
                    "Blocked #%d -> %s pending acknowledgment for #%d",
                    issue_number,
                    session_name,
                    blocking_issue,
                )
                return "awaiting_ack"

    # Pre-send reservation for issue deliveries to close the send-time race window.
    delivery_claim_id: int | None = None
    if delivery_kind == "issue" and _can_track_issue_delivery(db, issue_number, target_entity):
        claim_result = await db.claim_delivery_attempt(
            issue_number=issue_number,
            target_entity=target_entity,
            session_name=session_name,
            flow_name=flow_name,
        )
        if claim_result is None:
            return "already_delivered"
        if isinstance(claim_result, int):
            delivery_claim_id = claim_result

    # HTTP delivery targets bypass tmux intelligence entirely
    if is_http_target(session_name, config):
        from agent_backbone.jarvis import inject_message

        if await inject_message(
            config.jarvis.inject_url, message, sessions_url=config.jarvis.sessions_url
        ):
            await _persist_delivery_outcome(
                db,
                issue_number,
                target_entity,
                session_name,
                "delivered",
                flow_name,
                delivery_kind,
                delivery_claim_id,
            )
            await _clear_matching_queue_rows(
                db,
                session_name,
                message,
                delivery_kind,
                issue_number,
            )
            return "delivered"
        await _maybe_enqueue(
            session_name,
            message,
            issue_number,
            target_entity,
            flow_name,
            db,
            delivery_kind=delivery_kind,
        )
        await _persist_delivery_outcome(
            db,
            issue_number,
            target_entity,
            session_name,
            "delivery_failed",
            flow_name,
            delivery_kind,
            delivery_claim_id,
        )
        return "delivery_failed"

    profile = await get_session_intelligence(session_name, config, idle_since=idle_since)

    intelligence = profile.intelligence
    allow_same_issue_comment = delivery_kind == "comment" and _comment_matches_active_issue(
        issue_number,
        profile.current_issue,
    )

    # Determine deliverability
    if intelligence == SessionIntelligence.OFFLINE:
        await _maybe_enqueue(
            session_name,
            message,
            issue_number,
            target_entity,
            flow_name,
            db,
            delivery_kind=delivery_kind,
        )
        await _persist_delivery_outcome(
            db,
            issue_number,
            target_entity,
            session_name,
            "offline",
            flow_name,
            delivery_kind,
            delivery_claim_id,
        )
        return "offline"

    if intelligence == SessionIntelligence.PLAN_WAITING and not allow_same_issue_comment:
        if delivery_kind != "issue":
            await _maybe_enqueue(
                session_name,
                message,
                issue_number,
                target_entity,
                flow_name,
                db,
                delivery_kind=delivery_kind,
            )
        await _persist_delivery_outcome(
            db,
            issue_number,
            target_entity,
            session_name,
            "plan_waiting",
            flow_name,
            delivery_kind,
            delivery_claim_id,
        )
        return "plan_waiting"

    if intelligence == SessionIntelligence.PERMISSION_WAITING and not allow_same_issue_comment:
        if delivery_kind != "issue":
            await _maybe_enqueue(
                session_name,
                message,
                issue_number,
                target_entity,
                flow_name,
                db,
                delivery_kind=delivery_kind,
            )
        await _persist_delivery_outcome(
            db,
            issue_number,
            target_entity,
            session_name,
            "permission_waiting",
            flow_name,
            delivery_kind,
            delivery_claim_id,
        )
        return "permission_waiting"

    if intelligence == SessionIntelligence.AGENT_WORKING and not allow_same_issue_comment:
        if delivery_kind != "issue":
            await _maybe_enqueue(
                session_name,
                message,
                issue_number,
                target_entity,
                flow_name,
                db,
                delivery_kind=delivery_kind,
            )
        await _persist_delivery_outcome(
            db,
            issue_number,
            target_entity,
            session_name,
            "agent_working",
            flow_name,
            delivery_kind,
            delivery_claim_id,
        )
        return "agent_working"

    if intelligence == SessionIntelligence.IDLE_GRACE and not allow_same_issue_comment:
        if delivery_kind != "issue":
            await _maybe_enqueue(
                session_name,
                message,
                issue_number,
                target_entity,
                flow_name,
                db,
                delivery_kind=delivery_kind,
            )
        await _persist_delivery_outcome(
            db,
            issue_number,
            target_entity,
            session_name,
            "grace_period",
            flow_name,
            delivery_kind,
            delivery_claim_id,
        )
        return "grace_period"

    if intelligence == SessionIntelligence.COPY_MODE:
        profile = await _recover_copy_mode(session_name, config, profile, idle_since=idle_since)
        intelligence = profile.intelligence
        if intelligence == SessionIntelligence.COPY_MODE:
            await _maybe_enqueue(
                session_name,
                message,
                issue_number,
                target_entity,
                flow_name,
                db,
                delivery_kind=delivery_kind,
            )
            await _persist_delivery_outcome(
                db,
                issue_number,
                target_entity,
                session_name,
                "delivery_failed",
                flow_name,
                delivery_kind,
                delivery_claim_id,
            )
            return "delivery_failed"

    if intelligence == SessionIntelligence.USER_INTERACTING and not priority:
        await _maybe_enqueue(
            session_name,
            message,
            issue_number,
            target_entity,
            flow_name,
            db,
            delivery_kind=delivery_kind,
        )
        await _persist_delivery_outcome(
            db,
            issue_number,
            target_entity,
            session_name,
            "user_interacting",
            flow_name,
            delivery_kind,
            delivery_claim_id,
        )
        return "user_interacting"

    # Deliverable: IDLE_READY, UNKNOWN, or priority-bypassed COPY_MODE/USER_INTERACTING
    if await send_message(session_name, message, runtime_hint=profile.runtime):
        await _persist_delivery_outcome(
            db,
            issue_number,
            target_entity,
            session_name,
            "delivered",
            flow_name,
            delivery_kind,
            delivery_claim_id,
        )
        await _clear_matching_queue_rows(
            db,
            session_name,
            message,
            delivery_kind,
            issue_number,
        )
        return "delivered"

    # Delivery failed
    await _maybe_enqueue(
        session_name,
        message,
        issue_number,
        target_entity,
        flow_name,
        db,
        delivery_kind=delivery_kind,
    )
    await _persist_delivery_outcome(
        db,
        issue_number,
        target_entity,
        session_name,
        "delivery_failed",
        flow_name,
        delivery_kind,
        delivery_claim_id,
    )
    return "delivery_failed"


async def _maybe_enqueue(
    session_name: str,
    message: str,
    issue_number: int | None,
    target_entity: str | None,
    flow_name: str,
    db: BackboneDB | None = None,
    *,
    delivery_kind: str = "issue",
) -> None:
    """Enqueue a message to SQLite when the delivery kind supports deferral."""
    if delivery_kind == "issue" and (issue_number is None or target_entity is None):
        return
    if db is None:
        log.debug("No DB provided to _maybe_enqueue — skipping enqueue for %s", session_name)
        return
    try:
        await db.enqueue_message(
            session_name=session_name,
            message=message,
            issue_number=issue_number,
            target_entity=target_entity,
            delivery_kind=delivery_kind,
            flow_name=flow_name,
        )
        log.info(
            "Enqueued message for %s (#%s) via %s",
            session_name,
            issue_number,
            flow_name or "unknown",
        )
    except Exception:
        log.warning("Failed to enqueue message for %s (non-fatal)", session_name)


async def list_sessions_full(config: BackboneConfig) -> list[dict]:
    """List all tmux sessions enriched with intelligence and agent state.

    Combines list_sessions_rich() metadata (name, windows, created, attached)
    with get_session_intelligence() for each session. Returns a list of dicts
    with all rich fields plus 'intelligence' and 'agent_state' string fields.
    """
    from agent_backbone.services.terminal import list_sessions_rich

    sessions = await list_sessions_rich()
    results: list[dict] = []
    for session in sessions:
        name = session["name"]
        profile = await get_session_intelligence(name, config)
        enriched = dict(session)
        enriched["intelligence"] = str(profile.intelligence)
        enriched["agent_state"] = str(profile.agent_state)
        results.append(enriched)
    return results
