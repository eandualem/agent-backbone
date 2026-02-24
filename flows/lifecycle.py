"""Close-then-next flow: issue closed → query GitHub → deliver next.

When an issue is closed, determines which entity was the target,
queries GitHub for remaining open issues, and delivers the next one.
"""

from __future__ import annotations

import logging

from prefect import flow, task

from src.config import BackboneConfig
from src.dedup import is_recent_notification
from src.github import GitHubClient
from src.models import IssueData, IssueEvent
from src.notifications import format_next_issue_notification
from src.session_bridge import is_http_target, resolve_entity_session, safe_deliver
from src.tmux import session_exists

log = logging.getLogger(__name__)


async def _check_dependencies(issue_number: int) -> None:
    """Call dependency tracker — errors must not block lifecycle."""
    try:
        from flows.dependency_tracker import on_dependency_resolved

        await on_dependency_resolved(issue_number)
    except Exception:
        log.exception("Dependency tracker failed for #%d (non-fatal)", issue_number)


_ONBOARDING_TITLE_PREFIX = "[task] Verify onboarding infrastructure: "


async def _check_onboarding_chain(event: IssueEvent, config: BackboneConfig) -> None:
    """Sequential chain: when Brunel closes a verification issue, notify Leo.

    Detects onboarding verification issues by title prefix + for:brunel label.
    Extracts org/repo from the title and creates a follow-up issue for Leo.
    Errors are logged but never block the lifecycle flow.
    """
    try:
        title = event.issue.title
        if not title.startswith(_ONBOARDING_TITLE_PREFIX):
            return
        if "brunel" not in event.issue.labels.targets:
            return

        # Extract org/repo from title
        org_repo = title[len(_ONBOARDING_TITLE_PREFIX):].strip()
        if "/" not in org_repo:
            log.warning("Cannot parse org/repo from title: %s", title)
            return
        org, repo = org_repo.split("/", 1)

        if not config.github_token:
            log.info("No GitHub token — skipping onboarding chain for %s/%s", org, repo)
            return

        async with GitHubClient(config) as gh:
            await gh.create_issue(
                title=f"[task] Repository scaffolded and verified: {org}/{repo}",
                body=(
                    f"## Context\n"
                    f"The `{org}/{repo}` repository has been scaffolded"
                    f" by the automated pipeline\n"
                    f"and Brunel has verified the infrastructure"
                    f" (symlinks, registry, SDD).\n\n"
                    f"## Request\n"
                    f"The repo is ready for Phase 3 of the"
                    f" project-bootstrap pattern:\n"
                    f"write a comprehensive `PROJECT.md`"
                    f" synthesizing the strategic discussion.\n\n"
                    f"After writing PROJECT.md, notify Feynman"
                    f" (Phase 4) to configure agents.\n"
                    f"See your `project-bootstrap` skill"
                    f" for the full sequential chain.\n\n"
                    f"## References\n"
                    f"- Repo path: `~/ws/core/code/{org}/{repo}/`\n"
                    f"- Orchestration config:"
                    f" `~/orchestration/core/code/{org}/{repo}/`\n"
                    f"- Brunel verification issue: #{event.issue.number}\n"
                    f"- Bootstrap pattern:"
                    f" `orchestration/leo/.claude/skills/"
                    f"project-bootstrap/SKILL.md`\n"
                ),
                labels=["from:backbone", "for:leo", "task"],
            )
        log.info(
            "Onboarding chain: created Leo issue for %s/%s (Brunel closed #%d)",
            org, repo, event.issue.number,
        )
    except Exception:
        log.exception(
            "Onboarding chain handler failed for #%d (non-fatal)",
            event.issue.number,
        )


@task
async def find_next_issue(
    config: BackboneConfig, entity: str, exclude_number: int | None = None
) -> IssueData | None:
    """Query GitHub for the next open issue targeting an entity.

    Returns the highest-priority issue (blocking first, then oldest).
    Optionally excludes a specific issue number (e.g. a just-closed issue
    that may still appear as open due to GitHub eventual consistency).
    """
    label = f"for:{entity}"
    async with GitHubClient(config) as gh:
        issues = await gh.list_open_issues(label)

    if exclude_number is not None:
        pre_filter = len(issues)
        issues = [i for i in issues if i.number != exclude_number]
        if len(issues) < pre_filter:
            log.info(
                "Excluded just-closed #%d from next-issue query for %s",
                exclude_number,
                entity,
            )

    if not issues:
        log.info("No remaining open issues for %s (exclude=%s)", entity, exclude_number)
        return None

    log.info("Found %d open issue(s) for %s, next: #%d", len(issues), entity, issues[0].number)
    return issues[0]


@task
async def deliver_next(
    session_name: str,
    issue: IssueData,
    config: BackboneConfig,
    target_entity: str = "",
) -> str:
    """Deliver a next-issue notification via safe_deliver."""
    message = format_next_issue_notification(issue)
    return await safe_deliver(
        session_name,
        message,
        config,
        issue_number=issue.number,
        target_entity=target_entity,
        flow_name="issue-lifecycle",
    )


@flow(name="issue-lifecycle")
async def on_issue_closed(event: IssueEvent) -> dict:
    """Handle an issue_closed event by delivering the next queued issue.

    For each entity that was a target of the closed issue:
    1. Query GitHub for remaining open issues with that entity's for: label
    2. If issues remain and entity session is online, deliver the next one
    """
    result: dict[str, str] = {}  # entity → outcome
    config = BackboneConfig.from_toml()

    for target in event.issue.labels.targets:
        if target in config.entities.skip:
            result[target] = "skipped"
            continue

        # Resolve session name (no title extraction — lifecycle uses fallback directly)
        session_name = await resolve_entity_session(
            target, config, event.issue.title, use_title_extraction=False
        )

        if not session_name:
            result[target] = "no_session"
            continue

        # Check if session is reachable (HTTP targets are always reachable)
        if not is_http_target(session_name, config) and not await session_exists(session_name):
            log.info("Session '%s' offline — next issue delivered when online", session_name)
            result[target] = "offline"
            continue

        # Find the next issue (exclude just-closed issue — GitHub eventual consistency)
        next_issue = await find_next_issue(config, target, exclude_number=event.issue.number)
        if not next_issue:
            result[target] = "queue_empty"
            continue

        # Dedup: don't re-deliver an issue the entity was already notified about recently
        # This prevents duplicate "next issue" notifications when multiple closes happen
        if is_recent_notification(next_issue.number, session_name):
            log.info(
                "Suppressed duplicate next-issue notification for #%d → %s",
                next_issue.number,
                session_name,
            )
            result[target] = f"deduped_#{next_issue.number}"
            continue

        # Deliver it
        outcome = await deliver_next(session_name, next_issue, config, target_entity=target)
        if outcome == "delivered":
            result[target] = f"delivered_#{next_issue.number}"
        else:
            result[target] = outcome

    # Check if closing this issue unblocks any parent issues
    await _check_dependencies(event.issue.number)

    # Sequential onboarding chain: Brunel verification → Leo PROJECT.md
    await _check_onboarding_chain(event, config)

    log.info("Lifecycle complete: %s", result)
    return result
