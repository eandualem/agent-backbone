"""Tests for webhook utility functions and persistence dedup."""

from __future__ import annotations

import hashlib
import hmac

from agent_backbone.api.routes.webhook import verify_signature
from agent_backbone.models import EventType, IssueEvent
from agent_backbone.services.database import BackboneDB, build_engine
from agent_backbone.services.routing._dedup import clear as clear_dedup
from agent_backbone.services.routing._dedup import is_recent_notification


def _make_db() -> BackboneDB:
    return BackboneDB(build_engine("sqlite+aiosqlite:///:memory:"))


class TestVerifySignature:
    def test_valid_signature(self):
        secret = "test-secret"
        payload = b'{"action": "opened"}'
        sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        assert verify_signature(payload, sig, secret) is True

    def test_invalid_signature(self):
        assert verify_signature(b"payload", "sha256=wrong", "secret") is False

    def test_missing_signature(self):
        assert verify_signature(b"payload", None, "secret") is False


class TestIsDuplicate:
    def test_first_delivery(self):
        assert _make_db().is_duplicate("abc-123") is False

    def test_duplicate_delivery(self):
        db = _make_db()
        db.is_duplicate("abc-123")
        assert db.is_duplicate("abc-123") is True

    def test_empty_delivery_id(self):
        assert _make_db().is_duplicate("") is False

    def test_max_capacity(self):
        db = _make_db()
        for i in range(150):
            db.is_duplicate(f"delivery-{i}", max_ids=100)
        assert db.is_duplicate("delivery-0", max_ids=100) is False
        assert db.is_duplicate("delivery-149", max_ids=100) is True


class TestIsRecentNotification:
    def setup_method(self):
        clear_dedup()

    def test_first_notification(self):
        assert is_recent_notification("acme/app", 42, "ike") is False

    def test_duplicate_within_window(self):
        is_recent_notification("acme/app", 42, "ike", dedup_seconds=60)
        assert is_recent_notification("acme/app", 42, "ike", dedup_seconds=60) is True

    def test_different_issue_not_duplicate(self):
        is_recent_notification("acme/app", 42, "ike")
        assert is_recent_notification("acme/app", 43, "ike") is False
        assert is_recent_notification("acme/other", 42, "ike") is False

    def test_different_target_not_duplicate(self):
        is_recent_notification("acme/app", 42, "ike")
        assert is_recent_notification("acme/app", 42, "feynman") is False


class TestNormalizeEvent:
    def test_issue_opened(self):
        payload = {
            "action": "opened",
            "repository": {"full_name": "acme/agent-shell"},
            "issue": {
                "number": 42,
                "title": "[task] Test",
                "state": "open",
                "labels": [{"name": "from:leo"}, {"name": "for:ike"}, {"name": "task"}],
            },
        }
        event = IssueEvent.from_webhook("issues", "opened", payload, "delivery-1")
        assert event.event_type == EventType.ISSUE_OPENED
        assert event.issue.number == 42
        assert event.issue.labels.sender == "leo"
        assert "ike" in event.issue.labels.targets
        assert event.issue.repo_full_name == "acme/agent-shell"

    def test_issue_closed(self):
        payload = {
            "action": "closed",
            "issue": {"number": 10, "title": "[task] Done", "state": "closed", "labels": []},
        }
        assert (
            IssueEvent.from_webhook("issues", "closed", payload, "d").event_type
            == EventType.ISSUE_CLOSED
        )

    def test_comment_created(self):
        payload = {
            "action": "created",
            "issue": {"number": 42, "title": "[task] Test", "labels": [{"name": "for:ike"}]},
            "comment": {"body": "Hello", "user": {"login": "someone"}},
        }
        event = IssueEvent.from_webhook("issue_comment", "created", payload, "d")
        assert event.event_type == EventType.COMMENT_CREATED
        assert event.comment is not None
        assert event.comment.body == "Hello"

    def test_unknown_event(self):
        payload = {"action": "deleted", "issue": {"number": 1, "labels": []}}
        assert (
            IssueEvent.from_webhook("issues", "deleted", payload, "d").event_type
            == EventType.UNKNOWN
        )

    def test_pull_request_opened(self):
        payload = {
            "action": "opened",
            "repository": {"full_name": "acme/agent-backbone"},
            "pull_request": {
                "number": 73,
                "title": "Add repo-local webhook routing",
                "state": "open",
                "html_url": "https://github.com/acme/agent-backbone/pull/73",
                "labels": [],
            },
        }
        event = IssueEvent.from_webhook("pull_request", "opened", payload, "d")
        assert event.event_type == EventType.PULL_REQUEST_OPENED
        assert event.issue.number == 73
        assert event.issue.repo_full_name == "acme/agent-backbone"
