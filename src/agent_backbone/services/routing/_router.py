"""Core dispatch flow: event -> parse -> route -> deliver.

Receives normalized IssueEvent from gateway, resolves target sessions,
and delivers notifications via tmux. Handles issue_opened, issue_labeled,
and comment_created events. issue_closed events are routed to lifecycle.

State-aware: checks agent state before delivery. Busy agents get deferred.
"""

from __future__ import annotations

import logging

from prefect import flow, task

from agent_backbone.config import BackboneConfig
from agent_backbone.models import EventType, IssueEvent, parse_from_tag
from agent_backbone.services.agents._delivery_check import find_outgoing_comment
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.routing._delivery import safe_deliver
from agent_backbone.services.routing._format import (
    format_comment_notification,
    format_issue_notification,
)
from agent_backbone.services.routing._resolution import resolve_entity_session
from agent_backbone.services.routing.models import DispatchResult

log = logging.getLogger(__name__)


@task
async def resolve_session(target: str, issue_title: str, config: BackboneConfig) -> str | None:
    """Resolve a target entity to a tmux session name.

    Delegates to session_bridge.resolve_entity_session() for unified resolution.
    Kept as a Prefect @task for dashboard visibility.
    """
    return await resolve_entity_session(target, config, issue_title)


def _resolve_commenter_entity(event: IssueEvent) -> str | None:
    """Identify who made the comment.

    Primary: parse ``[from:X]`` tag from the first line of the comment body.
    Fallback: check the JSONL action log for a recent outgoing comment on this issue.
    """
    if event.comment and event.comment.body:
        entity = parse_from_tag(event.comment.body)
        if entity:
            return entity

    # JSONL fallback — within the 30s recency window
    originator = find_outgoing_comment(event.issue.number)
    return originator


def _compute_comment_targets(
    event: IssueEvent,
    commenter: str | None,
    skip_set: frozenset[str],
) -> list[str]:
    """Compute the set of entities to notify about a comment.

    Formula: {sender} ∪ {targets} - {commenter} - {skip_set}
    """
    all_parties: set[str] = set()
    all_parties.add(event.issue.labels.sender)
    all_parties.update(event.issue.labels.targets)

    # Remove commenter and skip set
    if commenter:
        all_parties.discard(commenter)
    all_parties -= skip_set

    return list(all_parties)


async def _deliver_to_entity(
    target: str,
    event: IssueEvent,
    message: str,
    config: BackboneConfig,
    db: BackboneDB,
    result: DispatchResult,
    is_blocking: bool,
) -> None:
    """Deliver a notification to a single entity with state checks and persistence."""
    session_name = await resolve_session(target, event.issue.title, config)
    if not session_name:
        log.warning("Could not resolve session for target '%s'", target)
        result.skipped.append(target)
        return

    outcome = await safe_deliver(
        session_name,
        message,
        config,
        db=db,
        issue_number=event.issue.number,
        target_entity=target,
        flow_name="issue-dispatcher",
        priority=is_blocking,
    )

    # Map safe_deliver outcomes to DispatchResult fields
    if outcome == "delivered":
        result.delivered.append(session_name)
    elif outcome == "offline":
        result.offline.append(session_name)
    elif outcome == "delivery_failed":
        result.offline.append(session_name)
    else:
        # agent_working, plan_waiting, copy_mode, user_interacting, grace_period
        result.deferred.append(session_name)

    log.info(
        "Decision: #%d → %s (%s) = %s",
        event.issue.number,
        target,
        session_name,
        outcome,
    )

    # Record delivery to SQLite for monitor awareness
    try:
        await db.record_delivery(
            issue_number=event.issue.number,
            target_entity=target,
            session_name=session_name,
            outcome=outcome,
            flow_name="issue-dispatcher",
        )
    except Exception:
        log.exception("Failed to record delivery (non-fatal)")


@flow(name="issue-dispatcher")
async def issue_dispatcher(
    event: IssueEvent, config: BackboneConfig, db: BackboneDB
) -> DispatchResult:
    """Dispatch a webhook event to target entity sessions.

    Routes issue_opened, issue_labeled, and comment_created events.
    issue_closed events should be routed to lifecycle.on_issue_closed instead.

    Checks agent state before delivery — busy agents are deferred.
    """
    result = DispatchResult()
    is_blocking = event.issue.labels.priority == "blocking"

    # --- Comment events: separate code path with expanded notify set ---
    if event.event_type == EventType.COMMENT_CREATED and event.comment:
        commenter = _resolve_commenter_entity(event)
        targets = _compute_comment_targets(event, commenter, config.entities.skip)
        message = format_comment_notification(
            event.issue, event.comment, commenter_entity=commenter
        )

        # Resolve commenter to a session for session-level self-suppression
        commenter_session: str | None = None
        if commenter:
            commenter_session = await resolve_session(commenter, event.issue.title, config)

        # Record acknowledgment for the commenter (they've engaged with the issue)
        if commenter:
            try:
                await db.record_acknowledgment(event.issue.number, commenter)
            except Exception:
                log.exception("Failed to record acknowledgment (non-fatal)")

        for target in targets:
            target_session = await resolve_session(target, event.issue.title, config)
            if not target_session:
                log.warning("Could not resolve session for target '%s'", target)
                result.skipped.append(target)
                continue

            # Session-level self-suppression: skip if target resolves to
            # the same session as the commenter (handles coding-agent overlap)
            if commenter_session and target_session == commenter_session:
                log.info(
                    "Suppressed comment self-notification for '%s' (session '%s') on #%d",
                    target,
                    target_session,
                    event.issue.number,
                )
                result.skipped.append(target)
                continue

            # Clear acknowledgment for the target (new info for them)
            try:
                await db.clear_acknowledgment(event.issue.number, target)
            except Exception:
                log.exception("Failed to clear acknowledgment (non-fatal)")

            await _deliver_to_entity(target, event, message, config, db, result, is_blocking)

        log.info(
            "Comment dispatch: %d delivered, %d skipped, %d offline, %d deferred",
            len(result.delivered),
            len(result.skipped),
            len(result.offline),
            len(result.deferred),
        )
        return result

    # --- Issue events (ISSUE_OPENED, ISSUE_LABELED) ---
    if event.event_type in (EventType.ISSUE_OPENED, EventType.ISSUE_LABELED):
        message = format_issue_notification(event.issue)
    else:
        log.info("Ignoring event type: %s", event.event_type)
        return result

    # Deliver to each target from for: labels
    for target in event.issue.labels.targets:
        if target in config.entities.skip:
            result.skipped.append(target)
            continue

        # Suppress self-notification on issue creation/labeling
        if target == event.issue.labels.sender:
            log.info("Suppressed self-notification for '%s' on #%d", target, event.issue.number)
            result.skipped.append(target)
            continue

        await _deliver_to_entity(target, event, message, config, db, result, is_blocking)

    log.info(
        "Dispatch: %d delivered, %d skipped, %d offline, %d deferred",
        len(result.delivered),
        len(result.skipped),
        len(result.offline),
        len(result.deferred),
    )
    return result
