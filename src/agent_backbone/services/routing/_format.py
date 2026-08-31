"""Message formatting for terminal notifications.

Every message that reaches an agent starts with a provenance envelope
(``[via:github issue:42]``, ``[via:backbone]``) so the agent — and anyone
reading its transcript — can tell where the text came from. Content after
the envelope is untrusted input from the tracker.
"""

from __future__ import annotations

from agent_backbone.models import CommentData, IssueData, parse_from_tag


def _repo_from_html_url(html_url: str) -> str:
    """Extract owner/repo from a GitHub issue URL."""
    parts = html_url.split("/")
    if len(parts) >= 5 and parts[2] == "github.com":
        return f"{parts[3]}/{parts[4]}"
    return ""


def _issue_ref(issue: IssueData) -> str:
    """Repository-qualified reference such as ``owner/repo#42``."""
    repo_full_name = issue.repo_full_name or _repo_from_html_url(issue.html_url)
    return f"{repo_full_name}#{issue.number}" if repo_full_name else f"#{issue.number}"


def _link(issue: IssueData) -> str:
    return f"Link: {issue.html_url}" if issue.html_url else ""


def format_issue_notification(issue: IssueData) -> str:
    """Format a new-issue notification for terminal delivery."""
    labels = issue.labels
    priority_str = f", {labels.priority}" if labels.priority else ""
    type_str = f" [{labels.issue_type}]" if labels.issue_type else ""

    return (
        f"[via:github issue:{issue.number}] "
        f'New issue targeting you: {_issue_ref(issue)}{type_str} "{issue.title}" '
        f"(from {labels.sender}{priority_str}). {_link(issue)}"
    ).rstrip()


def format_pull_request_notification(issue: IssueData) -> str:
    """Format a new-pull-request notification for repo-owner delivery."""
    repo_full_name = issue.repo_full_name or _repo_from_html_url(issue.html_url) or "unknown repo"
    return (
        f"[via:github pr:{issue.number}] "
        f'New pull request in {repo_full_name}: #{issue.number} "{issue.title}". '
        f"Review at: {issue.html_url}"
    )


def format_comment_notification(
    issue: IssueData,
    comment: CommentData,
    commenter_entity: str | None = None,
) -> str:
    """Format a new-comment notification for terminal delivery."""
    body = comment.body
    tag = parse_from_tag(body)
    if tag:
        idx = body.index("]") + 1
        body = body[idx:].lstrip()

    preview = body[:500].replace("\n", " ")
    if len(body) > 500:
        preview += "..."

    attribution = commenter_entity if commenter_entity else comment.user_login

    return (
        f"[via:github issue:{issue.number}] "
        f'New comment on {_issue_ref(issue)} "{issue.title}" from {attribution}: "{preview}" '
        f"{_link(issue)}"
    ).rstrip()


def format_unblock_notification(issue: IssueData) -> str:
    """Format notification when all sub-issues of a parent are resolved."""
    labels = issue.labels
    type_str = f" [{labels.issue_type}]" if labels.issue_type else ""

    return (
        f"[via:backbone] "
        f'Dependencies resolved for {_issue_ref(issue)}{type_str} "{issue.title}" '
        f"(from {labels.sender}). All sub-issues are now closed. {_link(issue)}"
    ).rstrip()


def format_stall_notification(
    session: str, issue_number: int, duration_minutes: int, entity: str
) -> str:
    """Format a stall escalation notification for the escalation target."""
    return (
        f"[via:backbone] "
        f"Agent {entity} ({session}) appears stalled on issue #{issue_number} "
        f"for {duration_minutes}m. Check the session and intervene if needed."
    )


def format_unexpected_offline_notification(session: str, entity: str, pending_count: int) -> str:
    """Format an unexpected-offline escalation notification."""
    issue_word = "issue" if pending_count == 1 else "issues"
    return (
        f"[via:backbone] "
        f"Agent {entity} ({session}) went offline unexpectedly with "
        f"{pending_count} pending {issue_word}. Session may need restart."
    )


def format_plan_notification(
    session: str,
    entity: str,
    plan_file: str,
    plan_title: str,
    issue_number: int | None = None,
) -> str:
    """Format plan-waiting notification for escalation-target delivery."""
    issue_str = f" (issue #{issue_number})" if issue_number else ""
    return (
        f"[via:backbone] Agent {entity} ({session}) created a plan{issue_str}. "
        f'Title: "{plan_title}". Plan file: {plan_file}'
    )


def format_watch_notification(issue: IssueData) -> str:
    """Informational notice for watchers (never queued as work)."""
    labels = issue.labels
    type_str = f" [{labels.issue_type}]" if labels.issue_type else ""
    targets = f" for {', '.join(labels.targets)}" if labels.targets else ""
    return (
        f"[via:github issue:{issue.number}] "
        f'FYI: new issue {_issue_ref(issue)}{type_str} "{issue.title}" '
        f"(from {labels.sender}{targets}). {_link(issue)}"
    ).rstrip()


def format_unassigned_notification(issue: IssueData, owners: list[str]) -> str:
    """An unlabelled issue in a repository with several owners."""
    return (
        f"[via:github issue:{issue.number}] "
        f'Unassigned issue {_issue_ref(issue)} "{issue.title}" (from {issue.labels.sender}). '
        f"Owners: {', '.join(owners)} — comment on it to claim it. {_link(issue)}"
    ).rstrip()


def format_closed_notification(issue: IssueData) -> str:
    """Tell the opener that an issue they opened was closed."""
    return (
        f"[via:github issue:{issue.number}] "
        f'Issue you opened was closed: {_issue_ref(issue)} "{issue.title}". {_link(issue)}'
    ).rstrip()


def format_next_issue_notification(issue: IssueData) -> str:
    """Format a close-then-next notification."""
    labels = issue.labels
    priority_str = f" [{labels.priority}]" if labels.priority else ""
    type_str = f" [{labels.issue_type}]" if labels.issue_type else ""

    return (
        f"[via:backbone] "
        f'Next issue in your queue: {_issue_ref(issue)}{type_str}{priority_str} "{issue.title}" '
        f"(from {labels.sender}). {_link(issue)}"
    ).rstrip()


class NotificationService:
    """Stateless notification formatting service implementing LifecycleAware."""

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def health_check(self) -> dict:
        return {"healthy": True, "service": "notifications"}
