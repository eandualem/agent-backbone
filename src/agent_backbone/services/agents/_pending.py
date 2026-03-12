"""Pending delivery logic: state-aware delivery loop."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from prefect import task

from agent_backbone.config import REPO_NAME_PATTERN, BackboneConfig
from agent_backbone.models import IssueData, parse_from_tag
from agent_backbone.services.agents._delivery_check import has_commented_on_issue, should_deliver
from agent_backbone.services.agents._inference import get_agent_state
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.routing._delivery import safe_deliver
from agent_backbone.services.routing._format import format_next_issue_notification

log = logging.getLogger(__name__)


@task
async def check_pending_issues(config: BackboneConfig, entity: str, gh: object) -> list[IssueData]:
    """Get all pending open issues for an entity, sorted by priority."""
    label = f"for:{entity}"
    return await gh.list_open_issues(label)


async def deliver_pending_issues(
    config: BackboneConfig,
    active_sessions: set[str],
    db: BackboneDB,
    gh: object,
) -> dict[str, str]:
    """State-aware delivery loop — deliver pending issues to idle agents.

    Returns a dict mapping entity → action taken.
    """
    result: dict[str, str] = {}
    comment_ack_cache: dict[int, set[str]] = {}

    async def is_acknowledged(
        issue_number: int,
        target_entity: str,
        sessions: list[str],
    ) -> bool:
        """Check acknowledgment via DB, local action log, then GitHub comments."""
        try:
            if await db.is_acknowledged(issue_number, target_entity):
                return True

            if any(has_commented_on_issue(issue_number, session_name) for session_name in sessions):
                await db.record_acknowledgment(issue_number, target_entity)
                return True

            acknowledged_entities = comment_ack_cache.get(issue_number)
            if acknowledged_entities is None:
                comments = await gh.list_comments(issue_number)
                acknowledged_entities = {
                    entity
                    for comment in comments
                    if comment.body
                    for entity in [parse_from_tag(comment.body)]
                    if entity
                }
                comment_ack_cache[issue_number] = acknowledged_entities

            if target_entity in acknowledged_entities:
                await db.record_acknowledgment(issue_number, target_entity)
                return True
        except Exception:
            log.exception(
                "Failed to check acknowledgment for #%d (non-fatal)",
                issue_number,
            )

        return False

    for entity in config.registry.all_entities:
        session_names = [
            session_name
            for session_name in config.registry.delivery_sessions_for(entity)
            if session_name in active_sessions
        ]
        if not session_names:
            continue

        multi_session = len(session_names) > 1
        deliverable_sessions: list[str] = []
        for session_name in session_names:
            result_key = f"{entity}:{session_name}" if multi_session else entity
            snapshot = await get_agent_state(
                config.agent_state.state_path,
                session_name,
                config.agent_state.stale_threshold_seconds,
            )

            # Monitor requires confirmed idle — only deliver to agents that are
            # definitively ready for work.
            if not should_deliver(snapshot.state, require_idle=True):
                result[result_key] = "deferred"
                log.info(
                    "Deferred delivery to %s (%s) (state=%s)",
                    entity,
                    session_name,
                    snapshot.state.value,
                )
                continue

            deliverable_sessions.append(session_name)

        if not deliverable_sessions:
            continue

        # Get all pending issues for this entity
        pending_issues = await check_pending_issues(config, entity, gh)
        if not pending_issues:
            if multi_session:
                for session_name in deliverable_sessions:
                    result[f"{entity}:{session_name}"] = "no_pending"
            else:
                result[entity] = "no_pending"
            continue
        queue_scope_issue_numbers = {issue.number for issue in pending_issues}

        # Iterate through pending issues — skip acknowledged and deliver the
        # first queue candidate independently to each active concrete session.
        candidate_processed = False
        for candidate in pending_issues:
            # Check if already acknowledged (agent commented on this issue)
            if await is_acknowledged(candidate.number, entity, session_names):
                log.debug("Skipping #%d for %s — acknowledged", candidate.number, entity)
                continue

            message = format_next_issue_notification(candidate)

            for session_name in deliverable_sessions:
                result_key = f"{entity}:{session_name}" if multi_session else entity

                # Skip if recently delivered to this concrete session.
                recently_delivered = False
                try:
                    recent = await db.query_deliveries(
                        issue_number=candidate.number,
                        target_entity=entity,
                        session_name=session_name,
                        outcome="delivered",
                        limit=1,
                    )
                    if recent and isinstance(recent, list):
                        delivered_at = datetime.fromisoformat(recent[0]["created_at"])
                        now = datetime.now(UTC)
                        if delivered_at.tzinfo is None:
                            delivered_at = delivered_at.replace(tzinfo=UTC)
                        age = (now - delivered_at).total_seconds()
                        if age < config.scheduling.monitor_interval_seconds * 2:
                            recently_delivered = True
                            log.info(
                                "Skipping #%d for %s (%s) — delivered %ds ago by %s",
                                candidate.number,
                                entity,
                                session_name,
                                int(age),
                                recent[0].get("flow_name", "?"),
                            )
                except Exception:
                    log.exception(
                        "Failed to check recent deliveries for #%d (non-fatal)",
                        candidate.number,
                    )

                if recently_delivered:
                    continue

                delivery_outcome = await safe_deliver(
                    session_name,
                    message,
                    config,
                    db=db,
                    issue_number=candidate.number,
                    target_entity=entity,
                    flow_name="agent-monitor",
                    enforce_issue_queue=True,
                    queue_scope_issue_numbers=queue_scope_issue_numbers,
                )
                if delivery_outcome == "delivered":
                    result[result_key] = f"delivered_#{candidate.number}"
                    log.info(
                        "Delivered pending issue #%d to %s (%s)",
                        candidate.number,
                        entity,
                        session_name,
                    )
                else:
                    result[result_key] = delivery_outcome

            session_keys = (
                [f"{entity}:{session_name}" for session_name in deliverable_sessions]
                if multi_session
                else [entity]
            )
            if not any(key in result for key in session_keys):
                result[entity] = "no_deliverable"

            candidate_processed = True
            break

        if not candidate_processed:
            session_keys = (
                [f"{entity}:{session_name}" for session_name in session_names]
                if multi_session
                else [entity]
            )
            if not any(key in result for key in session_keys):
                result[entity] = "no_deliverable"

    # --- Coding-agent sweep ---
    # Identify active coding-agent sessions (not named entities, not services)
    named_sessions = set(config.registry.sessions_map.values())
    coding_sessions = active_sessions - named_sessions - config.entities.service_sessions
    # Validate against known repo names (case-insensitive)
    repo_names_lower = {rn.lower() for rn in config.registry.repo_names}
    coding_sessions = {s for s in coding_sessions if s.lower() in repo_names_lower}

    if coding_sessions:
        # Fetch for:coding-agent issues once
        coding_issues: list[IssueData] = await check_pending_issues(config, "coding-agent", gh)
        coding_queue_scope_issue_numbers = {issue.number for issue in coding_issues}

        for session_name in sorted(coding_sessions):
            snapshot = await get_agent_state(
                config.agent_state.state_path,
                session_name,
                config.agent_state.stale_threshold_seconds,
            )
            if not should_deliver(snapshot.state, require_idle=True):
                result[f"coding:{session_name}"] = "deferred"
                log.info(
                    "Deferred coding-agent delivery to %s (state=%s)",
                    session_name,
                    snapshot.state.value,
                )
                continue

            # Match issues by repo name in title
            delivered = False
            for candidate in coding_issues:
                match = REPO_NAME_PATTERN.match(candidate.title or "")
                if not match or match.group(1).lower() != session_name.lower():
                    continue

                # Check ack
                if await is_acknowledged(candidate.number, "coding-agent", [session_name]):
                    continue

                # Check recently delivered
                recently_delivered = False
                try:
                    recent = await db.query_deliveries(
                        issue_number=candidate.number,
                        target_entity="coding-agent",
                        outcome="delivered",
                        limit=1,
                    )
                    if recent:
                        delivered_at = datetime.fromisoformat(recent[0]["created_at"])
                        now = datetime.now(UTC)
                        if delivered_at.tzinfo is None:
                            delivered_at = delivered_at.replace(tzinfo=UTC)
                        age = (now - delivered_at).total_seconds()
                        if age < config.scheduling.monitor_interval_seconds * 2:
                            recently_delivered = True
                except Exception:
                    log.exception(
                        "Failed to check recent deliveries for #%d (non-fatal)",
                        candidate.number,
                    )

                if recently_delivered:
                    continue

                # Deliver
                message = format_next_issue_notification(candidate)
                delivery_outcome = await safe_deliver(
                    session_name,
                    message,
                    config,
                    db=db,
                    issue_number=candidate.number,
                    target_entity="coding-agent",
                    flow_name="agent-monitor",
                    enforce_issue_queue=True,
                    queue_scope_issue_numbers=coding_queue_scope_issue_numbers,
                )
                if delivery_outcome == "delivered":
                    result[f"coding:{session_name}"] = f"delivered_#{candidate.number}"
                    log.info(
                        "Delivered coding-agent issue #%d to %s",
                        candidate.number,
                        session_name,
                    )
                elif delivery_outcome in {"already_delivered", "awaiting_ack"}:
                    result[f"coding:{session_name}"] = delivery_outcome
                else:
                    result[f"coding:{session_name}"] = delivery_outcome
                delivered = True
                break

            if not delivered and f"coding:{session_name}" not in result:
                result[f"coding:{session_name}"] = "no_deliverable"

    return result
