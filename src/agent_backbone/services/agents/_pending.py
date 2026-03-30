"""Pending delivery logic: state-aware delivery loop."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from agent_backbone.config import REPO_NAME_PATTERN, BackboneConfig
from agent_backbone.models import IssueData, acknowledgment_entities, parse_from_tag
from agent_backbone.services.agents._delivery_check import has_commented_on_issue, should_deliver
from agent_backbone.services.agents.interface import StateService
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.messaging import deliver_message

log = logging.getLogger(__name__)


def _format_next_issue(issue: IssueData) -> str:
    """Format a next-issue notification for tmux delivery."""
    labels = issue.labels
    priority_str = f" [{labels.priority}]" if labels.priority else ""
    type_str = f" [{labels.issue_type}]" if labels.issue_type else ""
    return (
        f"[via:backbone] "
        f'Next issue in your queue: #{issue.number}{type_str}{priority_str} "{issue.title}" '
        f"(from {labels.sender})"
    )


async def check_pending_issues(config: BackboneConfig, entity: str, gh: object) -> list[IssueData]:
    """Get all pending open issues for an entity, sorted by priority."""
    return await gh.list_open_issues(f"for:{entity}")


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
    comment_ack_cache: dict[tuple[str, int], set[str]] = {}
    state_svc = StateService(db=db)

    async def is_acknowledged(
        issue_number: int,
        target_entity: str,
        sessions: list[str],
        *,
        repo_full_name: str | None = None,
    ) -> bool:
        """Check acknowledgment via DB, local action log, then GitHub comments."""
        ack_entities = acknowledgment_entities(target_entity, repo_full_name)
        try:
            for entity in ack_entities:
                if await db.is_acknowledged(repo_full_name, issue_number, entity):
                    return True

            if any(
                has_commented_on_issue(issue_number, session_name, repo_full_name=repo_full_name)
                for session_name in sessions
            ):
                await db.record_acknowledgment(repo_full_name, issue_number, target_entity)
                return True

            cache_key = (repo_full_name or "", issue_number)
            acknowledged_entities = comment_ack_cache.get(cache_key)
            if acknowledged_entities is None:
                comments = await gh.list_comments(issue_number, repo_full_name=repo_full_name)
                acknowledged_entities = {
                    entity
                    for comment in comments
                    if comment.body
                    for entity in [parse_from_tag(comment.body)]
                    if entity
                }
                comment_ack_cache[cache_key] = acknowledged_entities

            if any(entity in acknowledged_entities for entity in ack_entities):
                await db.record_acknowledgment(repo_full_name, issue_number, target_entity)
                return True
        except Exception:
            log.exception(
                "Failed to check acknowledgment for #%d (non-fatal)",
                issue_number,
            )

        return False

    async def was_recently_delivered(
        issue_number: int,
        repo_full_name: str | None,
        target_entity: str,
        session_name: str,
    ) -> bool:
        """Whether the same target/session got this issue very recently."""
        try:
            # Query without outcome filter — outcomes may be prefixed
            # (e.g. "comment_delivered") so exact match misses them.
            recent = await db.query_deliveries(
                repo_full_name=repo_full_name,
                issue_number=issue_number,
                target_entity=target_entity,
                session_name=session_name,
                limit=10,
            )
            if recent and isinstance(recent, list):
                for row in recent:
                    outcome = row.get("outcome", "")
                    if not outcome.endswith(("delivered", "retried")):
                        continue
                    delivered_at = datetime.fromisoformat(row["created_at"])
                    now = datetime.now(UTC)
                    if delivered_at.tzinfo is None:
                        delivered_at = delivered_at.replace(tzinfo=UTC)
                    age = (now - delivered_at).total_seconds()
                    if age < config.scheduling.monitor_interval_seconds * 2:
                        log.info(
                            "Skipping #%d for %s (%s) — delivered %ds ago by %s",
                            issue_number,
                            target_entity,
                            session_name,
                            int(age),
                            row.get("flow_name", "?"),
                        )
                        return True
        except Exception:
            log.exception(
                "Failed to check recent deliveries for #%d (non-fatal)",
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
            snapshot = await state_svc.get_state(session_name)

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

        # Iterate through pending issues — skip acknowledged and deliver the
        # first queue candidate independently to each active concrete session.
        candidate_processed = False
        for candidate in pending_issues:
            # Check if already acknowledged (agent commented on this issue)
            if await is_acknowledged(
                candidate.number,
                entity,
                session_names,
                repo_full_name=candidate.repo_full_name,
            ):
                log.debug("Skipping #%d for %s — acknowledged", candidate.number, entity)
                continue

            message = _format_next_issue(candidate)

            for session_name in deliverable_sessions:
                result_key = f"{entity}:{session_name}" if multi_session else entity

                # Skip if recently delivered to this concrete session.
                if await was_recently_delivered(
                    candidate.number,
                    candidate.repo_full_name,
                    entity,
                    session_name,
                ):
                    continue

                delivery_outcome = await deliver_message(
                    session_name,
                    message,
                    config,
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

        for session_name in sorted(coding_sessions):
            snapshot = await state_svc.get_state(session_name)
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
                if await is_acknowledged(
                    candidate.number,
                    "coding-agent",
                    [session_name],
                    repo_full_name=candidate.repo_full_name,
                ):
                    continue

                # Check recently delivered
                if await was_recently_delivered(
                    candidate.number,
                    candidate.repo_full_name,
                    "coding-agent",
                    session_name,
                ):
                    continue

                # Deliver
                message = _format_next_issue(candidate)
                delivery_outcome = await deliver_message(
                    session_name,
                    message,
                    config,
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

        # Repo-local issue sweep for active code repos.
        for session_name in sorted(coding_sessions):
            prior_result = result.get(f"coding:{session_name}")
            if prior_result not in (None, "no_pending", "no_deliverable"):
                continue

            snapshot = await state_svc.get_state(session_name)
            if not should_deliver(snapshot.state, require_idle=True):
                result[f"repo:{session_name}"] = "deferred"
                log.info(
                    "Deferred repo-local delivery to %s (state=%s)",
                    session_name,
                    snapshot.state.value,
                )
                continue

            repo_full_name = f"{config.github.owner}/{session_name}"
            repo_issues = await gh.list_issues(state="open", repo_full_name=repo_full_name)
            if not repo_issues:
                result[f"repo:{session_name}"] = "no_pending"
                continue

            delivered = False
            for candidate in repo_issues:
                if await is_acknowledged(
                    candidate.number,
                    session_name,
                    [session_name],
                    repo_full_name=repo_full_name,
                ):
                    continue

                if await was_recently_delivered(
                    candidate.number,
                    repo_full_name,
                    session_name,
                    session_name,
                ):
                    continue

                message = _format_next_issue(candidate)
                delivery_outcome = await deliver_message(
                    session_name,
                    message,
                    config,
                )
                if delivery_outcome == "delivered":
                    result[f"repo:{session_name}"] = f"delivered_#{candidate.number}"
                    log.info(
                        "Delivered repo-local issue #%d to %s",
                        candidate.number,
                        session_name,
                    )
                else:
                    result[f"repo:{session_name}"] = delivery_outcome
                delivered = True
                break

            if not delivered and f"repo:{session_name}" not in result:
                result[f"repo:{session_name}"] = "no_deliverable"

    return result
