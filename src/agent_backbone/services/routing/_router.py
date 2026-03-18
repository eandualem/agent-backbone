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
    format_pull_request_notification,
)
from agent_backbone.services.routing._resolution import (
    resolve_entity_session,
    resolve_entity_sessions,
)
from agent_backbone.services.routing._targets import (
    list_open_queue_for_target,
    resolve_event_targets,
)
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
    fallback_targets: list[str] | None = None,
) -> list[str]:
    """Compute the set of entities to notify about a comment.

    Formula: {sender} ∪ {targets} - {commenter} - {skip_set}
    """
    all_parties: set[str] = set()
    if event.issue.labels.sender != "unknown":
        all_parties.add(event.issue.labels.sender)
    all_parties.update(event.issue.labels.targets)
    if not all_parties and fallback_targets:
        all_parties.update(fallback_targets)

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
    enforce_issue_queue: bool = False,
    queue_scope_issue_numbers: set[int] | None = None,
    delivery_kind: str = "issue",
    session_names: list[str] | None = None,
) -> None:
    """Deliver a notification to a single entity with state checks and persistence."""
    if session_names is None:
        session_names = await resolve_entity_sessions(target, config, event.issue.title)

    if not session_names:
        log.warning("Could not resolve session for target '%s'", target)
        result.skipped.append(target)
        return

    for session_name in session_names:
        outcome = await safe_deliver(
            session_name,
            message,
            config,
            db=db,
            issue_number=event.issue.number,
            target_entity=target,
            flow_name="issue-dispatcher",
            priority=is_blocking,
            enforce_issue_queue=enforce_issue_queue,
            queue_scope_issue_numbers=queue_scope_issue_numbers,
            delivery_kind=delivery_kind,
        )

        # Map safe_deliver outcomes to DispatchResult fields
        if outcome == "delivered":
            result.delivered.append(session_name)
        elif outcome == "already_delivered":
            result.skipped.append(session_name)
        elif outcome == "offline":
            result.offline.append(session_name)
        elif outcome == "delivery_failed":
            result.offline.append(session_name)
        else:
            # awaiting_ack, agent_working, plan_waiting,
            # user_interacting, grace_period, unknown_state
            result.deferred.append(session_name)

        log.info(
            "Decision: #%d → %s (%s) = %s",
            event.issue.number,
            target,
            session_name,
            outcome,
        )


@flow(name="issue-dispatcher")
async def issue_dispatcher(
    event: IssueEvent,
    config: BackboneConfig,
    db: BackboneDB,
    gh: object | None = None,
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
        fallback_targets = resolve_event_targets(event, config)
        targets = _compute_comment_targets(
            event,
            commenter,
            config.entities.skip,
            fallback_targets=fallback_targets,
        )
        message = format_comment_notification(
            event.issue, event.comment, commenter_entity=commenter
        )

        # Resolve commenter to a session for session-level self-suppression
        commenter_sessions: set[str] = set()
        if commenter:
            commenter_sessions = set(
                await resolve_entity_sessions(commenter, config, event.issue.title)
            )

        # Record acknowledgment for the commenter (they've engaged with the issue)
        if commenter:
            try:
                await db.record_acknowledgment(event.issue.number, commenter)
            except Exception:
                log.exception("Failed to record acknowledgment (non-fatal)")

        for target in targets:
            target_sessions = await resolve_entity_sessions(target, config, event.issue.title)
            if not target_sessions:
                log.warning("Could not resolve session for target '%s'", target)
                result.skipped.append(target)
                continue

            # Session-level self-suppression: skip if target resolves to
            # the same session as the commenter (handles coding-agent overlap)
            deliverable_sessions = [
                session_name
                for session_name in target_sessions
                if session_name not in commenter_sessions
            ]
            suppressed_sessions = [
                session_name
                for session_name in target_sessions
                if session_name in commenter_sessions
            ]
            if not deliverable_sessions:
                for suppressed in suppressed_sessions:
                    log.info(
                        "Suppressed comment self-notification for '%s' (session '%s') on #%d",
                        target,
                        suppressed,
                        event.issue.number,
                    )
                result.skipped.append(target)
                continue

            for suppressed in suppressed_sessions:
                log.info(
                    "Suppressed comment self-notification for '%s' (session '%s') on #%d",
                    target,
                    suppressed,
                    event.issue.number,
                )
                result.skipped.append(suppressed)

            # Clear acknowledgment for the target (new info for them)
            try:
                await db.clear_acknowledgment(event.issue.number, target)
            except Exception:
                log.exception("Failed to clear acknowledgment (non-fatal)")

            await _deliver_to_entity(
                target,
                event,
                message,
                config,
                db,
                result,
                is_blocking,
                delivery_kind="comment",
                session_names=deliverable_sessions,
            )

        log.info(
            "Comment dispatch: %d delivered, %d skipped, %d offline, %d deferred",
            len(result.delivered),
            len(result.skipped),
            len(result.offline),
            len(result.deferred),
        )
        return result

    # --- Issue events (ISSUE_OPENED, ISSUE_LABELED) ---
    delivery_kind = "issue"
    if event.event_type in (EventType.ISSUE_OPENED, EventType.ISSUE_LABELED):
        message = format_issue_notification(event.issue)
    elif event.event_type == EventType.PULL_REQUEST_OPENED:
        message = format_pull_request_notification(event.issue)
        delivery_kind = "pull_request"
    else:
        log.info("Ignoring event type: %s", event.event_type)
        return result

    # Deliver to each target from for: labels
    queue_scope_cache: dict[str, set[int]] = {}
    targets = resolve_event_targets(event, config)
    for target in targets:
        if target in config.entities.skip:
            result.skipped.append(target)
            continue

        # Suppress self-notification on issue creation/labeling
        if target == event.issue.labels.sender:
            log.info("Suppressed self-notification for '%s' on #%d", target, event.issue.number)
            result.skipped.append(target)
            continue

        queue_scope_issue_numbers: set[int] | None = None
        if gh is not None and delivery_kind == "issue":
            try:
                open_issues = await list_open_queue_for_target(
                    config,
                    target,
                    gh,
                    issue_repo_full_name=event.issue.repo_full_name,
                )
                queue_scope_issue_numbers = {issue.number for issue in open_issues}
                queue_scope_cache[target] = queue_scope_issue_numbers
            except Exception:
                log.exception("Failed to load queue scope for %s (non-fatal)", target)

        await _deliver_to_entity(
            target,
            event,
            message,
            config,
            db,
            result,
            is_blocking,
            enforce_issue_queue=delivery_kind == "issue",
            queue_scope_issue_numbers=queue_scope_cache.get(target, queue_scope_issue_numbers),
            delivery_kind=delivery_kind,
        )

    log.info(
        "Dispatch: %d delivered, %d skipped, %d offline, %d deferred",
        len(result.delivered),
        len(result.skipped),
        len(result.offline),
        len(result.deferred),
    )
    return result
