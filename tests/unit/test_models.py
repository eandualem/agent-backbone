"""Tests for the GitHub event model."""

from __future__ import annotations

from agent_backbone.models import EventType, IssueData, IssueEvent


class TestLinkedIssues:
    def test_closing_keywords(self):
        body = "Closes #3, fixes #5 and resolves: #3.\nRelated to #9."
        assert IssueData(number=1, repo_full_name="a/b", body=body).linked_issues() == [3, 5]

    def test_empty_body(self):
        assert IssueData(number=1, repo_full_name="a/b").linked_issues() == []


class TestFromWebhook:
    def test_pull_request_carries_body_and_head_branch(self):
        payload = {
            "repository": {"full_name": "acme/app"},
            "pull_request": {
                "number": 12,
                "title": "feat",
                "state": "open",
                "labels": [],
                "body": "Closes #4",
                "head": {"ref": "feat/x", "repo": {"full_name": "forker/app"}},
            },
        }
        event = IssueEvent.from_webhook("pull_request", "opened", payload, "d-1")
        assert event.event_type == EventType.PULL_REQUEST_OPENED
        assert event.issue.head_ref == "feat/x"
        assert event.issue.head_repo == "forker/app"
        assert event.issue.linked_issues() == [4]

    def test_an_issue_without_a_body_is_fine(self):
        payload = {"repository": {"full_name": "acme/app"}, "issue": {"number": 1, "body": None}}
        event = IssueEvent.from_webhook("issues", "opened", payload, "d-2")
        assert event.issue.body == "" and event.issue.head_ref == ""
