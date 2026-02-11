"""Message formatting for tmux notifications.

Ported from webhook-receiver.py with additions for close-then-next.
"""

from __future__ import annotations

from src.models import CommentData, IssueData


def format_issue_notification(issue: IssueData) -> str:
    """Format a new-issue notification for tmux delivery."""
    labels = issue.labels
    priority_str = f", {labels.priority}" if labels.priority else ""
    type_str = f" [{labels.issue_type}]" if labels.issue_type else ""

    return (
        f'New issue targeting you: #{issue.number}{type_str} "{issue.title}" '
        f"(from {labels.sender}{priority_str}). "
        f'Review with: mcp__github__issue_read(method:"get", owner:"eandualem", '
        f'repo:"orchestration", issue_number:{issue.number})'
    )


def format_comment_notification(issue: IssueData, comment: CommentData) -> str:
    """Format a new-comment notification for tmux delivery."""
    preview = comment.body[:120].replace("\n", " ")
    if len(comment.body) > 120:
        preview += "..."

    return (
        f'New comment on issue #{issue.number} from {comment.user_login}: "{preview}" '
        f'Review with: mcp__github__issue_read(method:"get_comments", owner:"eandualem", '
        f'repo:"orchestration", issue_number:{issue.number})'
    )


def format_next_issue_notification(issue: IssueData) -> str:
    """Format a close-then-next notification — delivered when an issue is closed
    and there's another pending issue for the same entity."""
    labels = issue.labels
    priority_str = f" [{labels.priority}]" if labels.priority else ""
    type_str = f" [{labels.issue_type}]" if labels.issue_type else ""

    return (
        f'Next issue in your queue: #{issue.number}{type_str}{priority_str} "{issue.title}" '
        f"(from {labels.sender}). "
        f'Review with: mcp__github__issue_read(method:"get", owner:"eandualem", '
        f'repo:"orchestration", issue_number:{issue.number})'
    )
