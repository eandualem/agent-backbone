"""Repo-aware target and queue helpers for routing flows."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from agent_backbone.models import EventType, IssueData, IssueEvent
from agent_backbone.services.routing._priority import compute_priority_score

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig

log = logging.getLogger(__name__)


def default_repo_full_name(config: BackboneConfig) -> str:
    """Return the backbone's default GitHub repository."""
    return f"{config.github.owner}/{config.github.repo}"


def monitored_repo_full_names(config: BackboneConfig) -> tuple[str, ...]:
    """All repositories whose issues are visible to backbone delivery."""
    repos = {default_repo_full_name(config)}
    repos.update(f"{config.github.owner}/{repo_name}" for repo_name in config.registry.repo_names)
    return tuple(sorted(repos))


def repo_name_from_full_name(repo_full_name: str) -> str:
    """Extract the repository name from an owner/repo string."""
    if "/" not in repo_full_name:
        return ""
    return repo_full_name.split("/", 1)[1]


def repo_target_for_issue(issue: IssueData, config: BackboneConfig) -> str | None:
    """Return the repo session target for a non-default repo issue/PR."""
    repo_full_name = issue.repo_full_name or default_repo_full_name(config)
    if repo_full_name == default_repo_full_name(config):
        return None

    repo_name = repo_name_from_full_name(repo_full_name)
    if not repo_name or repo_name not in config.registry.repo_names:
        return None
    return repo_name


def resolve_event_targets(event: IssueEvent, config: BackboneConfig) -> list[str]:
    """Resolve delivery targets for an event.

    Orchestration issues continue to use explicit ``for:`` labels.
    Repo-local issue and pull-request events fall back to the repo session.
    """
    repo_target = repo_target_for_issue(event.issue, config)

    if event.event_type == EventType.PULL_REQUEST_OPENED:
        return [repo_target] if repo_target else []

    if event.issue.labels.targets:
        return list(event.issue.labels.targets)

    issue_events = {
        EventType.ISSUE_OPENED,
        EventType.ISSUE_LABELED,
        EventType.ISSUE_CLOSED,
        EventType.COMMENT_CREATED,
    }
    if repo_target and event.event_type in issue_events:
        return [repo_target]
    return []


def repo_full_name_for_target(
    target: str,
    config: BackboneConfig,
    *,
    issue_repo_full_name: str = "",
) -> str | None:
    """Resolve the GitHub repo to query for a delivery target."""
    if issue_repo_full_name:
        return issue_repo_full_name
    if target in config.registry.repo_names:
        return f"{config.github.owner}/{target}"
    return None


def _sort_issue_queue(issues: list[IssueData], config: BackboneConfig) -> list[IssueData]:
    issues.sort(
        key=lambda issue: (
            -compute_priority_score(issue, config.priority_scoring),
            issue.number,
            issue.repo_full_name,
        )
    )
    return issues


async def _list_open_issues_across_managed_repos(
    config: BackboneConfig,
    label: str,
    gh: object,
) -> list[IssueData]:
    repo_full_names = monitored_repo_full_names(config)
    results = await asyncio.gather(
        *(gh.list_open_issues(label, repo_full_name=repo_full_name) for repo_full_name in repo_full_names),
        return_exceptions=True,
    )

    issues: list[IssueData] = []
    for repo_full_name, result in zip(repo_full_names, results, strict=True):
        if isinstance(result, Exception):
            log.warning(
                "Failed to load %s queue from %s: %s",
                label,
                repo_full_name,
                result,
            )
            continue
        issues.extend(result)

    return _sort_issue_queue(issues, config)


async def list_open_queue_for_target(
    config: BackboneConfig,
    target: str,
    gh: object,
    *,
    issue_repo_full_name: str = "",
) -> list[IssueData]:
    """Load the open queue for a target from the correct repository."""
    repo_full_name = repo_full_name_for_target(
        target,
        config,
        issue_repo_full_name=issue_repo_full_name,
    )
    if repo_full_name and target in config.registry.repo_names:
        return await gh.list_issues(state="open", repo_full_name=repo_full_name)
    if repo_full_name:
        return await gh.list_open_issues(f"for:{target}", repo_full_name=repo_full_name)
    return await _list_open_issues_across_managed_repos(config, f"for:{target}", gh)
