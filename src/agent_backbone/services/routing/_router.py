"""Core dispatch: event -> audiences -> deliveries."""

from __future__ import annotations

import logging

from agent_backbone.config import BackboneConfig
from agent_backbone.models import DeliveryOutcome, EventType, IssueEvent, parse_from_tag
from agent_backbone.services.agents._delivery_check import find_outgoing_comment
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.routing._delivery import safe_deliver
from agent_backbone.services.routing._format import (
    format_comment_notification,
    format_issue_notification,
    format_pull_request_notification,
    format_unassigned_notification,
    format_watch_notification,
)
from agent_backbone.services.routing._resolution import resolve_entity_session
from agent_backbone.services.routing._targets import (
    comment_audience,
    issue_repo,
    list_open_queue_for_target,
    queue_scope,
    route_issue_event,
)
from agent_backbone.services.routing.models import DispatchResult

log = logging.getLogger(__name__)


def _resolve_commenter_entity(event: IssueEvent, config: BackboneConfig) -> str | None:
    """Who made the comment: ``[from:X]`` tag first, then the hook action log."""
    if event.comment and event.comment.body:
        entity = parse_from_tag(event.comment.body)
        if entity:
            return entity
    return find_outgoing_comment(
        event.issue.number, action_log=config.action_log_path, repo=issue_repo(event.issue)
    )


def _record(result: DispatchResult, session: str, outcome: str) -> None:
    if outcome == DeliveryOutcome.DELIVERED:
        result.delivered.append(session)
    elif outcome == DeliveryOutcome.ALREADY_DELIVERED:
        result.skipped.append(session)
    elif outcome in (DeliveryOutcome.OFFLINE, DeliveryOutcome.DELIVERY_FAILED):
        result.offline.append(session)
    else:
        result.deferred.append(session)


async def _deliver(
    target: str,
    message: str,
    event: IssueEvent,
    config: BackboneConfig,
    db: BackboneDB,
    result: DispatchResult,
    *,
    kind: str,
    priority: bool = False,
    enforce_issue_queue: bool = False,
    scope: set[tuple[str, int]] | None = None,
) -> None:
    session = resolve_entity_session(target, config)
    if session is None:
        log.warning("No session for target '%s'", target)
        result.skipped.append(target)
        return
    outcome = await safe_deliver(
        session,
        message,
        config,
        db=db,
        repo=issue_repo(event.issue),
        issue_number=event.issue.number,
        target_entity=target,
        flow_name="issue-dispatcher",
        priority=priority,
        enforce_issue_queue=enforce_issue_queue,
        queue_scope=scope,
        delivery_kind=kind,
    )
    _record(result, session, outcome)
    log.info(
        "Decision: %s#%d → %s (%s) = %s",
        issue_repo(event.issue),
        event.issue.number,
        target,
        kind,
        outcome,
    )


async def issue_dispatcher(
    event: IssueEvent,
    config: BackboneConfig,
    db: BackboneDB,
    gh: object | None = None,
) -> DispatchResult:
    """Dispatch an issue / comment / pull-request event to its audiences."""
    result = DispatchResult()
    repo = issue_repo(event.issue)
    is_blocking = event.issue.labels.priority == "blocking"

    # --- Comments ---
    if event.event_type == EventType.COMMENT_CREATED and event.comment:
        commenter = _resolve_commenter_entity(event, config)
        audience = comment_audience(event, commenter, config)
        message = format_comment_notification(
            event.issue, event.comment, commenter_entity=commenter
        )

        commenter_session: str | None = None
        if commenter:
            commenter_session = resolve_entity_session(commenter, config)
            try:
                await db.record_acknowledgment(event.issue.number, commenter, repo=repo)
            except Exception:
                log.exception("Failed to record acknowledgment (non-fatal)")

        for target in audience:
            session = resolve_entity_session(target, config)
            if session is None or session == commenter_session:
                result.skipped.append(target)
                continue
            try:
                await db.clear_acknowledgment(event.issue.number, target, repo=repo)
            except Exception:
                log.exception("Failed to clear acknowledgment (non-fatal)")
            await _deliver(
                target, message, event, config, db, result, kind="comment", priority=is_blocking
            )
        return result

    # --- Issues and pull requests ---
    routing = route_issue_event(event, config)

    if event.event_type == EventType.PULL_REQUEST_OPENED:
        message = format_pull_request_notification(event.issue)
        for target in routing.queue + routing.watch:
            await _deliver(target, message, event, config, db, result, kind="pull_request")
        return result

    if event.event_type not in (EventType.ISSUE_OPENED, EventType.ISSUE_LABELED):
        log.info("Ignoring event type: %s", event.event_type)
        return result

    sender = event.issue.labels.sender
    message = format_issue_notification(event.issue)

    # Explicit targets the backbone cannot route to (ignored or unknown names).
    for target in event.issue.labels.targets:
        if target not in routing.queue:
            if target not in config.routing.ignore_targets:
                log.warning("Unknown for: target '%s' on %s#%d", target, repo, event.issue.number)
            result.skipped.append(target)

    for target in routing.queue:
        if target == sender:
            log.info(
                "Suppressed self-notification for '%s' on %s#%d", target, repo, event.issue.number
            )
            result.skipped.append(target)
            continue
        scope: set[tuple[str, int]] | None = None
        if gh is not None:
            try:
                scope = queue_scope(await list_open_queue_for_target(config, target, gh))
            except Exception:
                log.exception("Failed to load queue scope for %s (non-fatal)", target)
        await _deliver(
            target,
            message,
            event,
            config,
            db,
            result,
            kind="issue",
            priority=is_blocking,
            enforce_issue_queue=True,
            scope=scope,
        )

    if routing.announce:
        announce = format_unassigned_notification(event.issue, routing.announce)
        for target in routing.announce:
            if target == sender:
                continue
            await _deliver(target, announce, event, config, db, result, kind="watch")

    if routing.watch:
        watch = format_watch_notification(event.issue)
        for target in routing.watch:
            if target == sender:
                continue
            await _deliver(target, watch, event, config, db, result, kind="watch")

    log.info(
        "Dispatch %s#%d: %d delivered, %d skipped, %d offline, %d deferred",
        repo,
        event.issue.number,
        len(result.delivered),
        len(result.skipped),
        len(result.offline),
        len(result.deferred),
    )
    return result
