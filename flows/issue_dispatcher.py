"""Core dispatch flow: event → parse → route → deliver.

Receives normalized IssueEvent from gateway, resolves target sessions,
and delivers notifications via tmux. Handles issue_opened, issue_labeled,
and comment_created events. issue_closed events are routed to lifecycle.py.

State-aware: checks agent state before delivery. Busy agents get deferred.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from prefect import flow, task

from src.agent_state import find_outgoing_comment, get_agent_state, should_deliver
from src.config import REPO_NAME_PATTERN, BackboneConfig
from src.models import EventType, IssueEvent, parse_from_tag
from src.notifications import format_comment_notification, format_issue_notification
from src.tmux import send_message, session_exists

log = logging.getLogger(__name__)

_config: BackboneConfig | None = None


def _get_config() -> BackboneConfig:
    global _config
    if _config is None:
        _config = BackboneConfig.from_toml()
    return _config


@dataclass
class DispatchResult:
    """Outcome of a dispatch operation."""

    delivered: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    offline: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)


@task
async def resolve_session(target: str, issue_title: str) -> str | None:
    """Resolve a target entity to a tmux session name.

    Named entities map directly. 'coding-agent' resolves by extracting
    repo name from issue title, falling back to Ike.
    """
    config = _get_config()
    if target == "coding-agent":
        match = REPO_NAME_PATTERN.match(issue_title)
        if match:
            repo_name = match.group(1)
            # Try candidates: full match, last segment, lowercase variants
            last_segment = repo_name.rsplit("/", 1)[-1] if "/" in repo_name else repo_name
            candidates = list(
                dict.fromkeys(
                    [
                        repo_name,
                        repo_name.lower(),
                        last_segment,
                        last_segment.lower(),
                    ]
                )
            )
            for candidate in candidates:
                if await session_exists(candidate):
                    log.info(
                        "Resolved coding-agent → repo session '%s' (from '%s')",
                        candidate,
                        repo_name,
                    )
                    return candidate
            log.info(
                "No session found for repo '%s' (tried: %s), using fallback",
                repo_name,
                candidates,
            )
        else:
            log.info("Could not extract repo name from title: %s", issue_title)
        fallback = config.entities.fallback.get(target)
        if fallback:
            log.info("Routing coding-agent → fallback '%s'", fallback)
            return fallback
        return None

    return config.entities.sessions.get(target)


@task
async def deliver_notification(session_name: str, message: str) -> bool:
    """Deliver a notification message to a tmux session."""
    return await send_message(session_name, message)


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
    result: DispatchResult,
    is_blocking: bool,
) -> None:
    """Deliver a notification to a single entity with state checks and persistence."""
    session_name = await resolve_session(target, event.issue.title)
    if not session_name:
        log.warning("Could not resolve session for target '%s'", target)
        result.skipped.append(target)
        return

    # Check agent state before delivery
    state_snap = await get_agent_state(
        config.agent_state.state_path,
        session_name,
        config.agent_state.stale_threshold_seconds,
    )
    busy_duration = None
    if state_snap.started_at is not None:
        busy_duration = time.time() - state_snap.started_at
    if not should_deliver(
        state_snap.state,
        is_blocking,
        busy_duration=busy_duration,
        busy_threshold=float(config.capacity_routing.busy_threshold_seconds),
    ):
        log.info(
            "Decision: #%d → %s (%s) = deferred (state=%s, blocking=%s)",
            event.issue.number,
            target,
            session_name,
            state_snap.state,
            is_blocking,
        )
        result.deferred.append(session_name)
        return

    if await deliver_notification(session_name, message):
        result.delivered.append(session_name)
        outcome = "delivered"
    else:
        result.offline.append(session_name)
        outcome = "offline"

    log.info(
        "Decision: #%d → %s (%s) = %s",
        event.issue.number,
        target,
        session_name,
        outcome,
    )

    # Record delivery to SQLite for monitor awareness
    try:
        from src.persistence import BackboneDB

        async with BackboneDB(str(config.delivery.db_file)) as db:
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
async def issue_dispatcher(event: IssueEvent) -> DispatchResult:
    """Dispatch a webhook event to target entity sessions.

    Routes issue_opened, issue_labeled, and comment_created events.
    issue_closed events should be routed to lifecycle.on_issue_closed instead.

    Checks agent state before delivery — busy agents are deferred.
    """
    config = _get_config()
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
            commenter_session = await resolve_session(commenter, event.issue.title)

        # Record acknowledgment for the commenter (they've engaged with the issue)
        if commenter:
            try:
                from src.persistence import BackboneDB

                async with BackboneDB(str(config.delivery.db_file)) as db:
                    await db.record_acknowledgment(event.issue.number, commenter)
            except Exception:
                log.exception("Failed to record acknowledgment (non-fatal)")

        for target in targets:
            target_session = await resolve_session(target, event.issue.title)
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
                from src.persistence import BackboneDB

                async with BackboneDB(str(config.delivery.db_file)) as db:
                    await db.clear_acknowledgment(event.issue.number, target)
            except Exception:
                log.exception("Failed to clear acknowledgment (non-fatal)")

            await _deliver_to_entity(target, event, message, config, result, is_blocking)

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

        await _deliver_to_entity(target, event, message, config, result, is_blocking)

    log.info(
        "Dispatch: %d delivered, %d skipped, %d offline, %d deferred",
        len(result.delivered),
        len(result.skipped),
        len(result.offline),
        len(result.deferred),
    )
    return result
