"""Message formatting for tmux notifications.

Ported from webhook-receiver.py with additions for close-then-next.
"""

from __future__ import annotations

from agent_backbone.models import CommentData, IssueData, parse_from_tag


def _repo_from_html_url(html_url: str) -> str:
    """Extract owner/repo from a GitHub issue URL."""
    parts = html_url.split("/")
    if len(parts) >= 5 and parts[2] == "github.com":
        return f"{parts[3]}/{parts[4]}"
    return ""


def _review_target(issue: IssueData) -> tuple[str, str]:
    """Resolve owner/repo for GitHub MCP review commands."""
    repo_full_name = issue.repo_full_name or _repo_from_html_url(issue.html_url)
    if "/" in repo_full_name:
        owner, repo = repo_full_name.split("/", 1)
        if owner and repo:
            return owner, repo
    return ("eandualem", "orchestration")


def _review_with(issue: IssueData, method: str) -> str:
    """Build an MCP review command for an issue in its source repository."""
    owner, repo = _review_target(issue)
    return (
        f'Review with: mcp__github__issue_read(method:"{method}", '
        f'owner:"{owner}", repo:"{repo}", issue_number:{issue.number})'
    )


def format_digest(
    title: str,
    sessions: list[str],
    pending_counts: dict[str, int],
    notes: list[str] | None = None,
) -> str:
    """Format a digest message (morning summary / evening report).

    Args:
        title: Digest heading (e.g. "Morning Startup", "Evening Report")
        sessions: List of active session names
        pending_counts: entity → count of pending issues
        notes: Optional additional notes
    """
    lines = [f"[via:backbone] === {title} ==="]
    lines.append(f"Active sessions: {len(sessions)}")
    if sessions:
        lines.append("  " + ", ".join(sorted(sessions)))

    if pending_counts:
        lines.append("Pending issues:")
        for entity, count in sorted(pending_counts.items()):
            lines.append(f"  {entity}: {count}")
    else:
        lines.append("No pending issues.")

    if notes:
        lines.append("")
        for note in notes:
            lines.append(f"  {note}")

    return "\n".join(lines)


def format_issue_notification(issue: IssueData) -> str:
    """Format a new-issue notification for tmux delivery."""
    labels = issue.labels
    priority_str = f", {labels.priority}" if labels.priority else ""
    type_str = f" [{labels.issue_type}]" if labels.issue_type else ""

    return (
        f"[via:github issue:{issue.number}] "
        f'New issue targeting you: #{issue.number}{type_str} "{issue.title}" '
        f"(from {labels.sender}{priority_str}). "
        f"{_review_with(issue, 'get')}"
    )


def format_pull_request_notification(issue: IssueData) -> str:
    """Format a new pull-request notification for repo-local delivery."""
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
    """Format a new-comment notification for tmux delivery."""
    # Strip [from:] tag from preview — it's metadata, not content
    body = comment.body
    tag = parse_from_tag(body)
    if tag:
        # Remove the tag prefix (e.g. "[from:ike]") and any leading whitespace after it
        idx = body.index("]") + 1
        body = body[idx:].lstrip()

    preview = body[:500].replace("\n", " ")
    if len(body) > 500:
        preview += "..."

    # Attribution: use entity name when available, fall back to user_login
    attribution = commenter_entity if commenter_entity else comment.user_login

    return (
        f"[via:github issue:{issue.number}] "
        f'New comment on #{issue.number} "{issue.title}" from {attribution}: "{preview}" '
        f"{_review_with(issue, 'get_comments')}"
    )


def format_unblock_notification(issue: IssueData) -> str:
    """Format notification when all sub-issues of a parent are resolved."""
    labels = issue.labels
    type_str = f" [{labels.issue_type}]" if labels.issue_type else ""

    return (
        f"[via:backbone] "
        f'Dependencies resolved for #{issue.number}{type_str} "{issue.title}" '
        f"(from {labels.sender}). All sub-issues are now closed. "
        f"{_review_with(issue, 'get')}"
    )


def format_stall_notification(
    session: str, issue_number: int, duration_minutes: int, entity: str
) -> str:
    """Format a stall escalation notification for the escalation target."""
    return (
        f"[via:backbone] "
        f"Agent {entity} ({session}) appears stalled on issue #{issue_number} "
        f"for {duration_minutes}m. Check session and intervene if needed. "
        f'Inspect with: mcp__github__issue_read(method:"get", owner:"eandualem", '
        f'repo:"orchestration", issue_number:{issue_number})'
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
    """Format plan-waiting notification for orchestrator delivery."""
    issue_str = f" (issue #{issue_number})" if issue_number else ""
    return (
        f"[via:backbone] Agent {entity} ({session}) created a plan{issue_str}. "
        f'Title: "{plan_title}". Plan file: {plan_file}'
    )


def format_next_issue_notification(issue: IssueData) -> str:
    """Format a close-then-next notification — delivered when an issue is closed
    and there's another pending issue for the same entity."""
    labels = issue.labels
    priority_str = f" [{labels.priority}]" if labels.priority else ""
    type_str = f" [{labels.issue_type}]" if labels.issue_type else ""

    return (
        f"[via:backbone] "
        f'Next issue in your queue: #{issue.number}{type_str}{priority_str} "{issue.title}" '
        f"(from {labels.sender}). "
        f"{_review_with(issue, 'get')}"
    )


class NotificationService:
    """Stateless notification formatting service implementing LifecycleAware."""

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def health_check(self) -> dict:
        return {"healthy": True, "service": "notifications"}
