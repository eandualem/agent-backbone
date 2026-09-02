"""Pending delivery loop: give each idle agent the next issue in its queue."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agent_backbone.models import SUCCESS_OUTCOMES, DeliveryOutcome, parse_from_tag
from agent_backbone.services.agents import AgentState, has_commented_on_issue
from agent_backbone.services.routing import (
    format_next_issue_notification,
    is_acknowledged,
    list_open_queue_for_target,
    queue_scope,
    safe_deliver,
)

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.database import BackboneDB
    from agent_backbone.services.github import GitHubClient
    from agent_backbone.services.jobs.monitor import AgentStates

log = logging.getLogger(__name__)

SOURCE = "agent-monitor"


async def deliver_pending_issues(
    config: BackboneConfig,
    states: AgentStates,
    db: BackboneDB,
    gh: GitHubClient,
) -> dict[str, str]:
    """Deliver the next pending issue to every idle agent. Returns agent -> action."""
    result: dict[str, str] = {}
    comment_ack_cache: dict[tuple[str, int], set[str]] = {}

    async def acknowledged(repo: str, issue_number: int, name: str) -> bool:
        """DB record, then local action log, then GitHub comments.

        ``name`` is the agent: its ``for:`` target label and its tmux session.
        """
        try:
            if await is_acknowledged(db, repo, issue_number, name, name):
                return True
            if has_commented_on_issue(issue_number, name, config.action_log_path, repo=repo):
                await db.acks.record(issue_number, name, repo=repo)
                return True
            key = (repo, issue_number)
            acknowledged = comment_ack_cache.get(key)
            if acknowledged is None:
                comments = await gh.list_comments(issue_number, repo_full_name=repo)
                acknowledged = {
                    entity
                    for comment in comments
                    if comment.body
                    for entity in [parse_from_tag(comment.body)]
                    if entity
                }
                comment_ack_cache[key] = acknowledged
            if name in acknowledged:
                await db.acks.record(issue_number, name, repo=repo)
                return True
        except Exception:
            log.exception(
                "Failed to check acknowledgment for %s#%d (non-fatal)", repo, issue_number
            )
        return False

    async def was_recently_delivered(
        repo: str, issue_number: int, target: str, session: str
    ) -> bool:
        try:
            recent = await db.deliveries.query(
                issue_number=issue_number,
                target_entity=target,
                session_name=session,
                limit=10,
                repo=repo,
                kind="issue",
            )
            for row in recent or []:
                if (row.get("outcome") or "") not in SUCCESS_OUTCOMES:
                    continue
                delivered_at = datetime.fromisoformat(row["created_at"])
                if delivered_at.tzinfo is None:
                    delivered_at = delivered_at.replace(tzinfo=UTC)
                age = (datetime.now(UTC) - delivered_at).total_seconds()
                if age < config.timing.monitor_interval_seconds * 2:
                    return True
        except Exception:
            log.exception(
                "Failed to check recent deliveries for %s#%d (non-fatal)", repo, issue_number
            )
        return False

    for name, snapshot in states.items():
        if snapshot.state != AgentState.IDLE:  # only a confirmed idle agent gets new work
            result[name] = "deferred"
            log.debug("Deferred delivery to %s (state=%s)", name, snapshot.state.value)
            continue

        pending_issues = await list_open_queue_for_target(config, name, gh)
        if not pending_issues:
            result[name] = "no_pending"
            continue
        scope = queue_scope(pending_issues)

        for candidate in pending_issues:
            repo = candidate.repo_full_name
            if await acknowledged(repo, candidate.number, name):
                continue
            if await was_recently_delivered(repo, candidate.number, name, name):
                result[name] = "recently_delivered"
                break

            outcome = await safe_deliver(
                name,
                format_next_issue_notification(candidate),
                config,
                db=db,
                repo=repo,
                issue_number=candidate.number,
                target_entity=name,
                source=SOURCE,
                enforce_issue_queue=True,
                queue_scope=scope,
            )
            if outcome == DeliveryOutcome.DELIVERED:
                result[name] = f"delivered_#{candidate.number}"
                log.info("Delivered pending %s#%d to %s", repo, candidate.number, name)
            else:
                result[name] = outcome.value
            break
        else:
            result[name] = "no_deliverable"

    return result
