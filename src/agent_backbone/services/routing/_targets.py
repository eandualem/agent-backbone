"""Repo-aware target and queue helpers for routing."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_backbone.models import EventType, IssueData, IssueEvent

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig


def default_repo_full_name(config: BackboneConfig) -> str:
    """Return the configured coordination repository (may be empty)."""
    return config.github.repo


def repo_owners_for_issue(issue: IssueData, config: BackboneConfig) -> list[str]:
    """Agents that own the repository an issue lives in (excluding the default repo)."""
    repo_full_name = issue.repo_full_name or default_repo_full_name(config)
    if not repo_full_name or repo_full_name == default_repo_full_name(config):
        return []
    return [spec.name for spec in config.agents.for_repo(repo_full_name)]


def resolve_event_targets(event: IssueEvent, config: BackboneConfig) -> list[str]:
    """Resolve delivery targets for an event.

    Explicit ``for:`` labels win. Events in a repository owned by an agent
    (``[agents.<name>] repo = "owner/name"``) fall back to that agent.
    """
    owners = repo_owners_for_issue(event.issue, config)

    if event.event_type == EventType.PULL_REQUEST_OPENED:
        return owners

    if event.issue.labels.targets:
        return list(event.issue.labels.targets)

    return owners


def repo_full_name_for_target(
    target: str,
    config: BackboneConfig,
    *,
    issue_repo_full_name: str = "",
) -> str | None:
    """Resolve the GitHub repo to query for a delivery target."""
    if issue_repo_full_name:
        return issue_repo_full_name
    spec = config.agents.get(target)
    if spec is not None and spec.repo:
        return spec.repo
    return default_repo_full_name(config) or None


async def list_open_queue_for_target(
    config: BackboneConfig,
    target: str,
    gh: object,
    *,
    issue_repo_full_name: str = "",
) -> list[IssueData]:
    """Load the open queue for a target.

    The queue is the union of ``for:<target>`` issues in the coordination repo
    and, when the agent owns a repository, every open issue in that repository.
    """
    issues: list[IssueData] = []
    seen: set[tuple[str, int]] = set()

    def _add(items: list[IssueData]) -> None:
        for item in items:
            key = (item.repo_full_name, item.number)
            if key in seen:
                continue
            seen.add(key)
            issues.append(item)

    spec = config.agents.get(target)
    owned_repo = spec.repo if spec is not None else ""

    if issue_repo_full_name and owned_repo and issue_repo_full_name == owned_repo:
        _add(await gh.list_issues(state="open", repo_full_name=owned_repo))
        if config.github.enabled and config.github.repo != owned_repo:
            _add(await gh.list_open_issues(f"for:{target}", repo_full_name=config.github.repo))
        return issues

    if config.github.enabled:
        _add(await gh.list_open_issues(f"for:{target}", repo_full_name=config.github.repo))
    if owned_repo and owned_repo != config.github.repo:
        _add(await gh.list_issues(state="open", repo_full_name=owned_repo))
    elif issue_repo_full_name and issue_repo_full_name != config.github.repo:
        _add(await gh.list_open_issues(f"for:{target}", repo_full_name=issue_repo_full_name))
    return issues
