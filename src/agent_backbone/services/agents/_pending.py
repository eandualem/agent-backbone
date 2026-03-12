"""Pending delivery logic: state-aware delivery loop."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from prefect import task

from agent_backbone.config import REPO_NAME_PATTERN, BackboneConfig
from agent_backbone.models import IssueData
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

    for entity in config.registry.all_entities:
        session_name = config.registry.sessions_map.get(entity)
        if not session_name or session_name not in active_sessions:
            continue

        # Check agent state before delivery
        snapshot = await get_agent_state(
            config.agent_state.state_path,
            session_name,
            config.agent_state.stale_threshold_seconds,
        )

        # Monitor requires confirmed idle — only deliver to agents that are
        # definitively ready for work.
        if not should_deliver(snapshot.state, require_idle=True):
            result[entity] = "deferred"
            log.info("Deferred delivery to %s (state=%s)", entity, snapshot.state.value)
            continue

        # Get all pending issues for this entity
        pending_issues = await check_pending_issues(config, entity, gh)
        if not pending_issues:
            result[entity] = "no_pending"
            continue
        queue_scope_issue_numbers = {issue.number for issue in pending_issues}

        # Iterate through pending issues — skip acknowledged and recently delivered,
        # deliver the first one that's ready
        delivered = False
        for candidate in pending_issues:
            # Check if already acknowledged (agent commented on this issue)
            is_acked = False
            try:
                if await db.is_acknowledged(candidate.number, entity):
                    is_acked = True
                elif has_commented_on_issue(candidate.number, session_name):
                    await db.record_acknowledgment(candidate.number, entity)
                    is_acked = True
            except Exception:
                log.exception(
                    "Failed to check acknowledgment for #%d (non-fatal)",
                    candidate.number,
                )

            if is_acked:
                log.debug("Skipping #%d for %s — acknowledged", candidate.number, entity)
                continue

            # Skip if recently delivered by another flow
            recently_delivered = False
            try:
                recent = await db.query_deliveries(
                    issue_number=candidate.number,
                    target_entity=entity,
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
                        log.info(
                            "Skipping #%d for %s — delivered %ds ago by %s",
                            candidate.number,
                            entity,
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

            # Found an unacknowledged, undelivered issue — deliver it
            message = format_next_issue_notification(candidate)
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
                result[entity] = f"delivered_#{candidate.number}"
                log.info("Delivered pending issue #%d to %s", candidate.number, entity)
            elif delivery_outcome in {"already_delivered", "awaiting_ack"}:
                result[entity] = delivery_outcome
            else:
                result[entity] = delivery_outcome
            delivered = True
            break

        if not delivered and not result.get(entity):
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
                is_acked = False
                try:
                    if await db.is_acknowledged(candidate.number, "coding-agent"):
                        is_acked = True
                    elif has_commented_on_issue(candidate.number, session_name):
                        await db.record_acknowledgment(candidate.number, "coding-agent")
                        is_acked = True
                except Exception:
                    log.exception(
                        "Failed to check ack for coding-agent #%d (non-fatal)",
                        candidate.number,
                    )

                if is_acked:
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
