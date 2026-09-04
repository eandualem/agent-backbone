"""Core dispatch: event -> audiences -> deliveries."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agent_backbone.models import DeliveryOutcome, EventType, IssueEvent, parse_from_tag
from agent_backbone.services.agents.acknowledgement import (
    find_outgoing_comment,
    find_outgoing_pull_request,
)
from agent_backbone.services.routing._dedup import is_recent_notification
from agent_backbone.services.routing._delivery import safe_deliver
from agent_backbone.services.routing._format import (
    format_comment_notification,
    format_issue_notification,
    format_pull_request_notification,
    format_review_notification,
    format_unassigned_notification,
    format_watch_notification,
)
from agent_backbone.services.routing._resolution import resolve_entity_session
from agent_backbone.services.routing._targets import (
    comment_audience,
    list_open_queue_for_target,
    queue_scope,
    route_issue,
)
from agent_backbone.services.routing.models import DispatchResult

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.database import BackboneDB
    from agent_backbone.services.github import GitHubClient

log = logging.getLogger(__name__)

SOURCE = "issue-dispatcher"


def _resolve_commenter_entity(event: IssueEvent, config: BackboneConfig) -> str | None:
    """Who made the comment: ``[from:X]`` tag first, then the hook action log."""
    if event.comment and event.comment.body:
        entity = parse_from_tag(event.comment.body)
        if entity:
            return entity
    return find_outgoing_comment(
        event.issue.number, action_log=config.action_log_path, repo=event.issue.repo_full_name
    )


def _record(result: DispatchResult, session: str, outcome: DeliveryOutcome) -> None:
    if outcome == DeliveryOutcome.DELIVERED:
        result.delivered.append(session)
    elif outcome == DeliveryOutcome.ALREADY_DELIVERED:
        result.skipped.append(session)
    elif outcome in (DeliveryOutcome.OFFLINE, DeliveryOutcome.DELIVERY_FAILED):
        result.offline.append(session)
    else:
        result.deferred.append(session)


def _event_sender(event: IssueEvent) -> str:
    if event.comment:
        return event.comment.user_login
    if event.review:
        return event.review.user_login
    return ""


def _source_key(event: IssueEvent, kind: str) -> str | None:
    """The originating event's identity, so a queued copy is never stored twice."""
    repo = event.issue.repo_full_name
    if kind == "comment" and event.comment and event.comment.id:
        return f"comment:{repo}#{event.issue.number}:{event.comment.id}"
    if kind == "review" and event.review and event.review.id:
        return f"review:{repo}#{event.issue.number}:{event.review.id}"
    return None


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
    repo = event.issue.repo_full_name
    session = resolve_entity_session(target, config)
    if session is None:
        log.warning("No session for target '%s'", target)
        result.skipped.append(target)
        return
    if kind == "issue" and is_recent_notification(
        repo, event.issue.number, target, config.routing.notification_dedup_seconds
    ):
        log.info("Deduped %s#%d → %s (announced moments ago)", repo, event.issue.number, target)
        result.skipped.append(target)
        return
    outcome = await safe_deliver(
        session,
        message,
        config,
        db=db,
        repo=repo,
        issue_number=event.issue.number,
        target_entity=target,
        source=SOURCE,
        priority=priority,
        enforce_issue_queue=enforce_issue_queue,
        queue_scope=scope,
        delivery_kind=kind,
        sender=_event_sender(event),
        source_key=_source_key(event, kind),
    )
    _record(result, session, outcome)
    log.info("Decision: %s#%d → %s (%s) = %s", repo, event.issue.number, target, kind, outcome)


async def _dispatch_comment(
    event: IssueEvent, config: BackboneConfig, db: BackboneDB, result: DispatchResult
) -> None:
    repo = event.issue.repo_full_name
    commenter = _resolve_commenter_entity(event, config)
    audience = comment_audience(event.issue, commenter, config)
    message = format_comment_notification(event.issue, event.comment, commenter_entity=commenter)

    commenter_session: str | None = None
    if commenter:
        commenter_session = resolve_entity_session(commenter, config)
        try:
            await db.acks.record(event.issue.number, commenter, repo=repo)
        except Exception:
            log.exception("Failed to record acknowledgment (non-fatal)")

    for target in audience:
        session = resolve_entity_session(target, config)
        if session is None or session == commenter_session:
            result.skipped.append(target)
            continue
        try:
            await db.acks.clear(event.issue.number, target, repo=repo)
        except Exception:
            log.exception("Failed to clear acknowledgment (non-fatal)")
        await _deliver(
            target,
            message,
            event,
            config,
            db,
            result,
            kind="comment",
            priority=event.issue.labels.blocking,
        )


async def _dispatch_review(
    event: IssueEvent, config: BackboneConfig, db: BackboneDB, result: DispatchResult
) -> None:
    """A review reaches the pull request's parties, like a comment would.

    The reviewer is excluded when it is an agent (a ``[from:X]`` tag in the
    review body); a bot or human reviewer excludes nobody.
    """
    review = event.review
    if review is None:
        return
    reviewer = parse_from_tag(review.body)
    audience = comment_audience(event.issue, reviewer, config)
    message = format_review_notification(event.issue, review)
    reviewer_session = resolve_entity_session(reviewer, config) if reviewer else None
    for target in audience:
        session = resolve_entity_session(target, config)
        if session is None or session == reviewer_session:
            result.skipped.append(target)
            continue
        await _deliver(
            target,
            message,
            event,
            config,
            db,
            result,
            kind="review",
            priority=event.issue.labels.blocking,
        )


async def _dispatch_pull_request(
    event: IssueEvent, config: BackboneConfig, db: BackboneDB, result: DispatchResult
) -> None:
    """Owners and watchers hear about a pull request — except the agent that
    opened it (its hook logged the ``gh pr create``), for which the issues
    the pull request closes count as acknowledged instead."""
    repo = event.issue.repo_full_name
    opener = find_outgoing_pull_request(
        event.issue.head_repo,
        event.issue.head_ref,
        action_log=config.action_log_path,
        base_repo=repo,
    )
    if opener:
        for number in event.issue.linked_issues():
            try:
                await db.acks.record(number, opener, repo=repo)
            except Exception:
                log.exception("Failed to record acknowledgment (non-fatal)")
    routing = route_issue(event.issue, event.event_type, config)
    message = format_pull_request_notification(event.issue)
    for target in routing.queue + routing.watch:
        if opener and resolve_entity_session(target, config) == opener:
            result.skipped.append(target)
            continue
        await _deliver(target, message, event, config, db, result, kind="pull_request")


async def _dispatch_issue(
    event: IssueEvent,
    config: BackboneConfig,
    db: BackboneDB,
    gh: GitHubClient | None,
    result: DispatchResult,
) -> None:
    repo = event.issue.repo_full_name
    routing = route_issue(event.issue, event.event_type, config)
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
            priority=event.issue.labels.blocking,
            enforce_issue_queue=True,
            scope=scope,
        )

    if routing.announce:
        announce = format_unassigned_notification(event.issue, routing.announce)
        for target in routing.announce:
            if target != sender:
                await _deliver(target, announce, event, config, db, result, kind="watch")

    if routing.watch:
        watch = format_watch_notification(event.issue)
        for target in routing.watch:
            if target != sender:
                await _deliver(target, watch, event, config, db, result, kind="watch")


async def issue_dispatcher(
    event: IssueEvent,
    config: BackboneConfig,
    db: BackboneDB,
    gh: GitHubClient | None = None,
) -> DispatchResult:
    """Dispatch an issue / comment / pull-request event to its audiences."""
    result = DispatchResult()
    if event.event_type == EventType.COMMENT_CREATED and event.comment:
        await _dispatch_comment(event, config, db, result)
    elif event.event_type == EventType.REVIEW_SUBMITTED and event.review:
        await _dispatch_review(event, config, db, result)
    elif event.event_type == EventType.PULL_REQUEST_OPENED:
        await _dispatch_pull_request(event, config, db, result)
    elif event.event_type in (EventType.ISSUE_OPENED, EventType.ISSUE_LABELED):
        await _dispatch_issue(event, config, db, gh, result)
    else:
        log.info("Ignoring event type: %s", event.event_type)
        return result

    log.info(
        "Dispatch %s#%d: %d delivered, %d skipped, %d offline, %d deferred",
        event.issue.repo_full_name,
        event.issue.number,
        len(result.delivered),
        len(result.skipped),
        len(result.offline),
        len(result.deferred),
    )
    return result
