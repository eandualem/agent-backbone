"""Current GitHub resource validity shared by outbox and queue replay."""

from __future__ import annotations

from typing import TYPE_CHECKING

from httpx import HTTPStatusError

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
