"""Tests for agent_backbone/services/notifications."""

from __future__ import annotations

from agent_backbone.models import CommentData, IssueData, ParsedLabels
from agent_backbone.services.routing._format import (
    format_comment_notification,
    format_issue_notification,
    format_next_issue_notification,
    format_plan_notification,
    format_stall_notification,
    format_unblock_notification,
    format_unexpected_offline_notification,
)
from tests.conftest import TEST_REPO


class TestFormatIssueNotification:
    def test_basic_format(self, sample_issue):
        msg = format_issue_notification(sample_issue)
        assert msg.startswith("[via:github issue:42]")
        assert "#42" in msg
        assert "[task]" in msg
        assert "Update config" in msg
        assert "from leo" in msg

    def test_with_priority(self):
        labels = ParsedLabels(
            sender="ike", targets=["feynman"], issue_type="bug", priority="blocking"
        )
        issue = IssueData(number=7, repo_full_name=TEST_REPO, title="[bug] Critical", labels=labels)
        msg = format_issue_notification(issue)
        assert msg.startswith("[via:github issue:7]")
        assert "blocking" in msg
        assert "[bug]" in msg

    def test_no_type(self):
        labels = ParsedLabels(sender="leo", targets=["ike"])
        issue = IssueData(number=1, repo_full_name=TEST_REPO, title="Something", labels=labels)
        msg = format_issue_notification(issue)
        assert "[]" not in msg

    def test_uses_issue_repo_for_review_command(self):
        labels = ParsedLabels(sender="leo", targets=["ike"], issue_type="task")
        issue = IssueData(
            number=42,
            title="[task] Update config",
            labels=labels,
            html_url="https://github.com/WF/agent-shell/issues/42",
            repo_full_name="WF/agent-shell",
        )
        msg = format_issue_notification(issue)
        assert "WF/agent-shell#42" in msg


class TestFormatCommentNotification:
    def test_basic_format(self, sample_issue, sample_comment):
        msg = format_comment_notification(sample_issue, sample_comment)
        assert msg.startswith("[via:github issue:42]")
        assert "#42" in msg
        assert "someone" in msg
        assert "test comment" in msg
        # Issue title must appear in the notification
        assert '"[task] Update config"' in msg
        # Attribution falls back to user_login when no entity provided
        assert "from someone" in msg

    def test_truncation(self, sample_issue):
        long_body = "x" * 600
        comment = CommentData(body=long_body, user_login="test")
        msg = format_comment_notification(sample_issue, comment)
        assert "..." in msg
        # The preview is 500 characters including the ellipsis: 497 of body.
        assert "x" * 497 + "..." in msg
        assert "x" * 498 not in msg

    def test_newline_replacement(self, sample_issue):
        comment = CommentData(body="line1\nline2\nline3", user_login="test")
        msg = format_comment_notification(sample_issue, comment)
        assert "\n" not in msg  # Newlines replaced in preview

    def test_with_commenter_entity(self, sample_issue, sample_comment):
        msg = format_comment_notification(sample_issue, sample_comment, commenter_entity="ike")
        assert "from ike" in msg
        # Should use entity name, not user_login
        assert "from someone" not in msg

    def test_from_tag_stripped(self, sample_issue):
        comment = CommentData(body="[from:ike] Acknowledged.", user_login="someone")
        msg = format_comment_notification(sample_issue, comment)
        # The [from:] tag is metadata and should not appear in the preview
        assert "[from:ike]" not in msg
        # The actual content after the tag should appear
        assert "Acknowledged." in msg

    def test_issue_title_in_notification(self, sample_issue, sample_comment):
        msg = format_comment_notification(sample_issue, sample_comment)
        # Issue title is included in the notification format
        assert "[task] Update config" in msg
        # Verify the full pattern: New comment on #N "title" from attribution
        assert 'New comment on example/orchestration#42 "[task] Update config"' in msg

    def test_uses_issue_repo_for_review_command(self, sample_comment):
        issue = IssueData(
            number=42,
            title="[task] Update config",
            labels=ParsedLabels(sender="leo", targets=["ike"], issue_type="task"),
            html_url="https://github.com/WF/agent-shell/issues/42",
            repo_full_name="WF/agent-shell",
        )
        msg = format_comment_notification(issue, sample_comment)
        assert "WF/agent-shell#42" in msg


class TestFormatNextIssueNotification:
    def test_basic_format(self, sample_issue):
        msg = format_next_issue_notification(sample_issue)
        assert msg.startswith("[via:backbone]")
        assert "Next issue in your queue" in msg
        assert "#42" in msg
        assert "[task]" in msg
        assert "from leo" in msg

    def test_blocking_priority(self):
        labels = ParsedLabels(
            sender="ada", targets=["ike"], issue_type="spec-gap", priority="blocking"
        )
        issue = IssueData(
            number=99, repo_full_name=TEST_REPO, title="[spec-gap] Missing contract", labels=labels
        )
        msg = format_next_issue_notification(issue)
        assert msg.startswith("[via:backbone]")
        assert "[blocking]" in msg
        assert "[spec-gap]" in msg


class TestFormatUnblockNotification:
    def test_basic_format(self, sample_issue):
        msg = format_unblock_notification(sample_issue)
        assert msg.startswith("[via:backbone]")
        assert "Dependencies resolved" in msg
        assert "#42" in msg
        assert "[task]" in msg
        assert "All sub-issues are now closed" in msg

    def test_includes_sender(self):
        labels = ParsedLabels(sender="ada", targets=["ike"], issue_type="spec-gap")
        issue = IssueData(
            number=10, repo_full_name=TEST_REPO, title="[spec-gap] Missing spec", labels=labels
        )
        msg = format_unblock_notification(issue)
        assert msg.startswith("[via:backbone]")
        assert "from ada" in msg
        assert "[spec-gap]" in msg


class TestFormatStallNotification:
    def test_basic_format(self):
        msg = format_stall_notification("feynman", 42, 95, "feynman")
        assert msg.startswith("[via:backbone]")
        assert "feynman" in msg
        assert "#42" in msg
        assert "95m" in msg
        assert "stalled" in msg

    def test_single_line(self):
        msg = format_stall_notification("ike", 10, 120, "ike")
        assert "\n" not in msg


class TestFormatUnexpectedOfflineNotification:
    def test_basic_format(self):
        msg = format_unexpected_offline_notification("feynman", "feynman", 3)
        assert msg.startswith("[via:backbone]")
        assert "feynman" in msg
        assert "offline unexpectedly" in msg
        assert "3 pending issues" in msg

    def test_singular_issue(self):
        msg = format_unexpected_offline_notification("ada", "ada", 1)
        assert msg.startswith("[via:backbone]")
        assert "1 pending issue" in msg
        assert "issues" not in msg.replace("issue", "")

    def test_zero_pending(self):
        msg = format_unexpected_offline_notification("leo", "leo", 0)
        assert "0 pending issues" in msg

    def test_single_line(self):
        msg = format_unexpected_offline_notification("ike", "ike", 5)
        assert "\n" not in msg


class TestFormatPlanNotification:
    def test_basic_format(self):
        msg = format_plan_notification("agent-backbone", "bell", "/tmp/plan.md", "Add feature X")
        assert msg.startswith("[via:backbone]")
        assert "bell" in msg
        assert "agent-backbone" in msg
        assert "/tmp/plan.md" in msg
        assert "Add feature X" in msg

    def test_with_issue_number(self):
        msg = format_plan_notification(
            "feynman", "feynman", "/tmp/plan.md", "Refactor", issue_number=42
        )
        assert "(issue #42)" in msg

    def test_without_issue_number(self):
        msg = format_plan_notification("feynman", "feynman", "/tmp/plan.md", "Refactor")
        assert "issue" not in msg

    def test_single_line(self):
        msg = format_plan_notification(
            "agent-backbone", "bell", "/tmp/plan.md", "Add feature X", issue_number=99
        )
        assert "\n" not in msg


class TestReviewAnchorsAndPreviews:
    """#132: a review names its commit; previews carry text, not HTML comments."""

    def test_review_names_the_commit_it_looked_at(self):
        from agent_backbone.models import IssueData, ParsedLabels, ReviewData
        from agent_backbone.services.routing._format import format_review_notification

        issue = IssueData(
            number=131, title="effort", labels=ParsedLabels(), repo_full_name="acme/app"
        )
        review = ReviewData(
            id=1,
            body="Actionable comments posted: 2",
            user_login="coderabbitai[bot]",
            state="commented",
            commit_id="3d460d10e43d9cedd77da1fcd905c387ef164b60",
        )
        text = format_review_notification(issue, review)
        assert "(commented) of 3d460d1:" in text

    def test_preview_skips_html_comments_and_whitespace(self):
        from agent_backbone.models import CommentData, IssueData, ParsedLabels
        from agent_backbone.services.routing._format import format_comment_notification

        body = (
            "<!-- This is an auto-generated comment: summarize by coderabbit.ai -->\n"
            "<!-- review_stack_entry_start -->\n\n"
            "No actionable comments were generated in the recent review.\n\n"
            "<details><summary>Run configuration</summary>x</details>"
        )
        issue = IssueData(number=1, title="t", labels=ParsedLabels(), repo_full_name="acme/app")
        text = format_comment_notification(issue, CommentData(id=1, body=body, user_login="bot"))
        assert "<!--" not in text
        assert text.index("No actionable comments") < text.index("<details>")

    def test_review_parsed_from_the_webhook_carries_the_commit(self):
        from agent_backbone.models import IssueEvent

        payload = {
            "action": "submitted",
            "pull_request": {"number": 7, "title": "t", "state": "open", "labels": []},
            "review": {
                "id": 5,
                "body": "",
                "state": "APPROVED",
                "user": {"login": "rev"},
                "commit_id": "abc1234def",
                "submitted_at": "2026-09-05T05:19:28Z",
            },
        }
        event = IssueEvent.from_webhook("pull_request_review", "submitted", payload, "d1")
        assert event.review is not None
        assert event.review.commit_id == "abc1234def"
        assert event.review.submitted_at == "2026-09-05T05:19:28Z"

    def test_a_truncated_preview_never_exceeds_the_cap(self):
        from agent_backbone.services.routing._format import _preview

        assert len(_preview("x" * 501)) == 500 and _preview("x" * 501).endswith("...")
        assert _preview("x" * 500) == "x" * 500

    def test_stacked_unterminated_openers_are_linear(self):
        import time

        from agent_backbone.services.routing._format import _preview

        started = time.perf_counter()
        assert _preview("<!--" * 20000 + " tail") == ""
        assert _preview("<!-- a --> kept <!-- b --> too") == "kept too"
        assert time.perf_counter() - started < 1.0
