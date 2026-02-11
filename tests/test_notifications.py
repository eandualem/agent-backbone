"""Tests for src/notifications.py."""

from __future__ import annotations

from src.models import CommentData, IssueData, ParsedLabels
from src.notifications import (
    format_comment_notification,
    format_issue_notification,
    format_next_issue_notification,
)


class TestFormatIssueNotification:
    def test_basic_format(self, sample_issue):
        msg = format_issue_notification(sample_issue)
        assert "#42" in msg
        assert "[task]" in msg
        assert "Update config" in msg
        assert "from leo" in msg
        assert "mcp__github__issue_read" in msg
        assert "issue_number:42" in msg

    def test_with_priority(self):
        labels = ParsedLabels(sender="ike", targets=["feynman"], issue_type="bug", priority="blocking")
        issue = IssueData(number=7, title="[bug] Critical", labels=labels)
        msg = format_issue_notification(issue)
        assert "blocking" in msg
        assert "[bug]" in msg

    def test_no_type(self):
        labels = ParsedLabels(sender="leo", targets=["ike"])
        issue = IssueData(number=1, title="Something", labels=labels)
        msg = format_issue_notification(issue)
        assert "[]" not in msg


class TestFormatCommentNotification:
    def test_basic_format(self, sample_issue, sample_comment):
        msg = format_comment_notification(sample_issue, sample_comment)
        assert "#42" in msg
        assert "eandualem" in msg
        assert "test comment" in msg
        assert "get_comments" in msg

    def test_truncation(self, sample_issue):
        long_body = "x" * 200
        comment = CommentData(body=long_body, user_login="test")
        msg = format_comment_notification(sample_issue, comment)
        assert "..." in msg
        # The preview should be 120 chars + "..."
        assert "x" * 120 in msg

    def test_newline_replacement(self, sample_issue):
        comment = CommentData(body="line1\nline2\nline3", user_login="test")
        msg = format_comment_notification(sample_issue, comment)
        assert "\n" not in msg.split('Review')[0]  # Newlines replaced in preview


class TestFormatNextIssueNotification:
    def test_basic_format(self, sample_issue):
        msg = format_next_issue_notification(sample_issue)
        assert "Next issue in your queue" in msg
        assert "#42" in msg
        assert "[task]" in msg
        assert "from leo" in msg

    def test_blocking_priority(self):
        labels = ParsedLabels(
            sender="ada", targets=["ike"], issue_type="spec-gap", priority="blocking"
        )
        issue = IssueData(number=99, title="[spec-gap] Missing contract", labels=labels)
        msg = format_next_issue_notification(issue)
        assert "[blocking]" in msg
        assert "[spec-gap]" in msg
