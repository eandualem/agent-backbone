"""Pending delivery loop: give each idle agent the next issue in its queue."""

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
from agent_backbone.services.routing._targets import (
    issue_repo,
    list_open_queue_for_target,
    queue_scope,
)

log = logging.getLogger(__name__)


async def deliver_pending_issues(
    config: BackboneConfig,
    active_sessions: set[str],
    db: BackboneDB,
    gh: object,
) -> dict[str, str]:
    """Deliver the next pending issue to every idle agent. Returns agent -> action."""
    result: dict[str, str] = {}
    comment_ack_cache: dict[tuple[str, int], set[str]] = {}

    async def is_acknowledged(repo: str, issue_number: int, target: str, session_name: str) -> bool:
        """DB record, then local action log, then GitHub comments."""
        try:
            if await db.is_acknowledged(issue_number, target, repo=repo):
                return True
            if has_commented_on_issue(
                issue_number, session_name, config.action_log_path, repo=repo
            ):
                await db.record_acknowledgment(issue_number, target, repo=repo)
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
            if target in acknowledged:
                await db.record_acknowledgment(issue_number, target, repo=repo)
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
            recent = await db.query_deliveries(
                issue_number=issue_number,
                target_entity=target,
                session_name=session,
                limit=10,
                repo=repo,
                kind="issue",
            )
            for row in recent or []:
                if (row.get("outcome") or "") not in ("delivered", "retried"):
                    continue
                delivered_at = datetime.fromisoformat(row["created_at"])
                if delivered_at.tzinfo is None:
                    delivered_at = delivered_at.replace(tzinfo=UTC)
                age = (datetime.now(UTC) - delivered_at).total_seconds()
                if age < config.monitor.interval_seconds * 2:
                    return True
        except Exception:
            log.exception(
                "Failed to check recent deliveries for %s#%d (non-fatal)", repo, issue_number
            )
        return False

    for name in config.agents.names:
        if name not in active_sessions:
            continue

        snapshot = await get_agent_state(
            config.state_dir, name, config.agent_state.stale_threshold_seconds
        )
        if not should_deliver(snapshot.state):
            result[name] = "deferred"
            log.debug("Deferred delivery to %s (state=%s)", name, snapshot.state.value)
            continue

        pending_issues = await list_open_queue_for_target(config, name, gh)
        if not pending_issues:
            result[name] = "no_pending"
            continue
        scope = queue_scope(pending_issues)

        for candidate in pending_issues:
            repo = issue_repo(candidate)
            if await is_acknowledged(repo, candidate.number, name, name):
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
                flow_name="agent-monitor",
                enforce_issue_queue=True,
                queue_scope=scope,
            )
            if outcome == "delivered":
                result[name] = f"delivered_#{candidate.number}"
                log.info("Delivered pending %s#%d to %s", repo, candidate.number, name)
            else:
                result[name] = outcome
            break
        else:
            result[name] = "no_deliverable"

    return result
