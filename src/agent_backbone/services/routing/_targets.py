"""Repo-aware target and queue helpers for routing flows."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_backbone.models import EventType, IssueData, IssueEvent

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig


def default_repo_full_name(config: BackboneConfig) -> str:
    """Return the backbone's default GitHub repository."""
    return f"{config.github.owner}/{config.github.repo}"


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
    return await gh.list_open_issues(f"for:{target}", repo_full_name=repo_full_name)
