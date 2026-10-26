"""Pending delivery loop: give each idle agent the next issue in its queue."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agent_backbone.models import SUCCESS_OUTCOMES, DeliveryOutcome
from agent_backbone.services.agents import (
    AgentState,
    has_commented_on_issue,
    read_comment_receipts,
)
from agent_backbone.services.jobs.diagnostics import observe_job
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
    receipts: tuple[dict, ...] | None = None

    async def acknowledged(repo: str, issue_number: int, name: str) -> bool:
        """DB record, then exact successful publication in the local action log.

        ``name`` is the agent: its ``for:`` target label and its tmux session.
        """
        nonlocal receipts
        try:
            if await is_acknowledged(db, repo, issue_number, name, name):
                return True
            cleared, comment_id = await db.acks.reset_boundary(issue_number, name, repo=repo)
            since = datetime.fromisoformat(cleared).timestamp() if cleared else None
            if receipts is None:
                receipts = await asyncio.to_thread(
                    lambda: tuple(read_comment_receipts(config.action_log_path))
                )
            if await asyncio.to_thread(
                has_commented_on_issue,
                issue_number,
                name,
                config.action_log_path,
                repo=repo,
                since=since,
                reset_comment_id=comment_id,
                receipts=receipts,
            ):
                return await db.acks.record(
                    issue_number, name, repo=repo, not_cleared_since=cleared
                )
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

    async def deliver_one(name: str) -> str:
        nonlocal receipts
        pending_issues = await list_open_queue_for_target(config, name, gh, db=db)
        if not pending_issues:
            return "no_pending"
        scope = queue_scope(pending_issues)

        for candidate in pending_issues:
            repo = candidate.repo_full_name
            if await acknowledged(repo, candidate.number, name):
                continue
            if await was_recently_delivered(repo, candidate.number, name, name):
                return "recently_delivered"

            # A later candidate can have been acknowledged since this pass loaded receipts.
            receipts = None
            if await acknowledged(repo, candidate.number, name):
                continue

            outcome = (
                await safe_deliver(
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
            ).outcome
            if outcome == DeliveryOutcome.DELIVERED:
                log.info("Delivered pending %s#%d to %s", repo, candidate.number, name)
                return f"delivered_#{candidate.number}"
            return outcome.value
        return "no_deliverable"

    for name, snapshot in states.items():
        if snapshot.state != AgentState.IDLE:  # only a confirmed idle agent gets new work
            result[name] = "deferred"
            log.debug("Deferred delivery to %s (state=%s)", name, snapshot.state.value)
            continue
        error_type = None
        try:
            result[name] = await deliver_one(name)
        except Exception as exc:
            result[name] = "failed"
            log.exception("Pending issue delivery failed for %s (other agents continue)", name)
            error_type = type(exc).__name__
        try:
            await observe_job(
                db,
                source=SOURCE,
                stage="pending_issues",
                agent_name=name,
                error_type=error_type,
            )
        except Exception:
            log.exception("Failed to record pending issue diagnostic for %s", name)

    return result
