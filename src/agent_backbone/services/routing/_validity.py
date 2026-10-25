"""Current GitHub resource validity shared by outbox and queue replay."""

from __future__ import annotations

from typing import TYPE_CHECKING

from httpx import HTTPStatusError

from agent_backbone.models import EventType
from agent_backbone.services.routing._targets import comment_audience, route_issue

if TYPE_CHECKING:
    from agent_backbone.models import IssueData
    from agent_backbone.services.github import GitHubClient


async def current_notification_issue(
    gh: GitHubClient | None, repo: str, issue_number: int, *, source_key: str = ""
) -> tuple[IssueData | None, str | None]:
    """Read the current resource, or return why its notification is obsolete.

    Transient errors (including no client) propagate so the durable caller
    defers instead of discarding unverified work. Explicit closure notices
    remain valid only for the same closure of the resource.
    """
    if gh is None:
        raise RuntimeError("GitHub is unavailable; notification validity cannot be checked")
    try:
        issue = await gh.get_issue(issue_number, repo_full_name=repo)
    except HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None, "issue_deleted"
        raise
    if source_key.startswith("closed:"):
        if issue.state != "closed" or (
            issue.closed_at and not source_key.endswith(f"@{issue.closed_at}")
        ):
            return None, "superseded_closure"
    elif issue.state == "closed":
        return None, "issue_closed"
    return issue, None


async def notification_still_targets(
    issue, kind, target, config, db, *, source="", source_key=""
) -> bool:
    """Re-evaluate the original notification kind against current routing policy."""
    spec = config.agents.get(target)
    if issue is None or spec is None or spec.inbox_only or target in config.routing.ignore_targets:
        return False
    event = EventType.PULL_REQUEST_OPENED if kind == "pull_request" else EventType.ISSUE_OPENED
    routing = route_issue(issue, event, config)
    if kind == "issue":
        return target in routing.queue
    if kind == "watch":
        if source_key.startswith("closed:"):
            return target == issue.labels.sender and target not in routing.queue
        if source == "dependency-tracker":
            return target in issue.labels.targets
        return target in routing.watch + routing.announce
    if kind == "pull_request":
        return target in routing.queue + routing.watch
    if target in comment_audience(issue, None, config):
        return True
    if kind == "comment":
        swarm = await db.swarms.active_for_issue(issue.repo_full_name, issue.number)
        return isinstance(swarm, dict) and swarm.get("coordinator") == target
    return False
