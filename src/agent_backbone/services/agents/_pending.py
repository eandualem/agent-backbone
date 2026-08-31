"""Pending delivery logic: state-aware delivery loop for configured agents."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from agent_backbone.config import BackboneConfig
from agent_backbone.models import parse_from_tag
from agent_backbone.services.agents._delivery_check import has_commented_on_issue, should_deliver
from agent_backbone.services.agents._inference import get_agent_state
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.routing._delivery import safe_deliver
from agent_backbone.services.routing._format import format_next_issue_notification
from agent_backbone.services.routing._targets import list_open_queue_for_target

log = logging.getLogger(__name__)


async def deliver_pending_issues(
    config: BackboneConfig,
    active_sessions: set[str],
    db: BackboneDB,
    gh: object,
) -> dict[str, str]:
    """State-aware delivery loop — deliver the next pending issue to idle agents.

    Returns a dict mapping agent name → action taken.
    """
    result: dict[str, str] = {}
    comment_ack_cache: dict[tuple[str, int], set[str]] = {}

    async def is_acknowledged(
        issue_number: int,
        target_entity: str,
        session_name: str,
        *,
        repo_full_name: str | None = None,
    ) -> bool:
        """Check acknowledgment via DB, local action log, then GitHub comments."""
        try:
            if await db.is_acknowledged(issue_number, target_entity):
                return True

            if has_commented_on_issue(issue_number, session_name, config.action_log_path):
                await db.record_acknowledgment(issue_number, target_entity)
                return True

            cache_key = (repo_full_name or "", issue_number)
            acknowledged = comment_ack_cache.get(cache_key)
            if acknowledged is None:
                comments = await gh.list_comments(issue_number, repo_full_name=repo_full_name)
                acknowledged = {
                    entity
                    for comment in comments
                    if comment.body
                    for entity in [parse_from_tag(comment.body)]
                    if entity
                }
                comment_ack_cache[cache_key] = acknowledged

            if target_entity in acknowledged:
                await db.record_acknowledgment(issue_number, target_entity)
                return True
        except Exception:
            log.exception("Failed to check acknowledgment for #%d (non-fatal)", issue_number)

        return False

    async def was_recently_delivered(
        issue_number: int,
        target_entity: str,
        session_name: str,
    ) -> bool:
        """Whether the same target/session got this issue very recently."""
        try:
            recent = await db.query_deliveries(
                issue_number=issue_number,
                target_entity=target_entity,
                session_name=session_name,
                limit=10,
            )
            for row in recent or []:
                outcome = row.get("outcome", "")
                if not outcome.endswith(("delivered", "retried")):
                    continue
                delivered_at = datetime.fromisoformat(row["created_at"])
                if delivered_at.tzinfo is None:
                    delivered_at = delivered_at.replace(tzinfo=UTC)
                age = (datetime.now(UTC) - delivered_at).total_seconds()
                if age < config.monitor.interval_seconds * 2:
                    log.info(
                        "Skipping #%d for %s — delivered %ds ago by %s",
                        issue_number,
                        session_name,
                        int(age),
                        row.get("flow_name", "?"),
                    )
                    return True
        except Exception:
            log.exception("Failed to check recent deliveries for #%d (non-fatal)", issue_number)
        return False

    for name in config.agents.names:
        if name not in active_sessions:
            continue

        snapshot = await get_agent_state(
            config.state_dir,
            name,
            config.agent_state.stale_threshold_seconds,
        )
        if not should_deliver(snapshot.state, require_idle=True):
            result[name] = "deferred"
            log.info("Deferred delivery to %s (state=%s)", name, snapshot.state.value)
            continue

        pending_issues = await list_open_queue_for_target(config, name, gh)
        if not pending_issues:
            result[name] = "no_pending"
            continue
        queue_scope_issue_numbers = {issue.number for issue in pending_issues}

        for candidate in pending_issues:
            if await is_acknowledged(
                candidate.number, name, name, repo_full_name=candidate.repo_full_name or None
            ):
                log.debug("Skipping #%d for %s — acknowledged", candidate.number, name)
                continue

            if await was_recently_delivered(candidate.number, name, name):
                result[name] = "recently_delivered"
                break

            message = format_next_issue_notification(candidate)
            delivery_outcome = await safe_deliver(
                name,
                message,
                config,
                db=db,
                issue_number=candidate.number,
                target_entity=name,
                flow_name="agent-monitor",
                enforce_issue_queue=True,
                queue_scope_issue_numbers=queue_scope_issue_numbers,
            )
            if delivery_outcome == "delivered":
                result[name] = f"delivered_#{candidate.number}"
                log.info("Delivered pending issue #%d to %s", candidate.number, name)
            else:
                result[name] = delivery_outcome
            break
        else:
            result[name] = "no_deliverable"

    return result
