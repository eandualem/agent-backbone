"""Tests for webhook utility functions and persistence dedup."""

from __future__ import annotations

import hashlib
import hmac

from sqlalchemy.ext.asyncio import create_async_engine

from agent_backbone.api.webhook_utils import normalize_event, verify_signature
from agent_backbone.models import EventType
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.routing import clear as clear_dedup
from agent_backbone.services.routing import is_recent_notification


def _make_db() -> BackboneDB:
    """Create a BackboneDB with a lightweight engine for hot-cache-only tests."""
    return BackboneDB(create_async_engine("sqlite+aiosqlite:///:memory:"))


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
        db = _make_db()
        assert db.is_duplicate("abc-123") is False

    def test_duplicate_delivery(self):
        db = _make_db()
        db.is_duplicate("abc-123")
        assert db.is_duplicate("abc-123") is True

    def test_empty_delivery_id(self):
        db = _make_db()
        assert db.is_duplicate("") is False

    def test_max_capacity(self):
        db = _make_db()
        for i in range(150):
            db.is_duplicate(f"delivery-{i}", max_ids=100)
        # Oldest entries should be evicted
        assert db.is_duplicate("delivery-0", max_ids=100) is False  # Evicted, counts as new
        assert db.is_duplicate("delivery-149", max_ids=100) is True  # Still present


class TestIsRecentNotification:
    def setup_method(self):
        clear_dedup()

    def test_first_notification(self):
        assert is_recent_notification(42, "ike") is False

    def test_duplicate_within_window(self):
        is_recent_notification(42, "ike", dedup_seconds=60)
        assert is_recent_notification(42, "ike", dedup_seconds=60) is True

    def test_different_issue_not_duplicate(self):
        is_recent_notification(42, "ike")
        assert is_recent_notification(43, "ike") is False

    def test_different_target_not_duplicate(self):
        is_recent_notification(42, "ike")
        assert is_recent_notification(42, "feynman") is False


class TestNormalizeEvent:
    def test_issue_opened(self):
        payload = {
            "action": "opened",
            "repository": {"full_name": "WF/agent-shell"},
            "issue": {
                "number": 42,
                "title": "[task] Test",
                "state": "open",
                "labels": [{"name": "from:leo"}, {"name": "for:ike"}, {"name": "task"}],
            },
        }
        event = normalize_event("issues", "opened", payload, "delivery-1")
        assert event.event_type == EventType.ISSUE_OPENED
        assert event.issue.number == 42
        assert event.issue.labels.sender == "leo"
        assert "ike" in event.issue.labels.targets
        assert event.issue.repo_full_name == "WF/agent-shell"

    def test_issue_closed(self):
        payload = {
            "action": "closed",
            "issue": {
                "number": 10,
                "title": "[task] Done",
                "state": "closed",
                "labels": [{"name": "from:ike"}, {"name": "for:feynman"}],
            },
        }
        event = normalize_event("issues", "closed", payload, "delivery-2")
        assert event.event_type == EventType.ISSUE_CLOSED

    def test_comment_created(self):
        payload = {
            "action": "created",
            "issue": {
                "number": 42,
                "title": "[task] Test",
                "labels": [{"name": "for:ike"}],
            },
            "comment": {
                "body": "Hello",
                "user": {"login": "eandualem"},
            },
        }
        event = normalize_event("issue_comment", "created", payload, "delivery-3")
        assert event.event_type == EventType.COMMENT_CREATED
        assert event.comment is not None
        assert event.comment.body == "Hello"

    def test_unknown_event(self):
        payload = {"action": "deleted", "issue": {"number": 1, "labels": []}}
        event = normalize_event("issues", "deleted", payload, "delivery-4")
        assert event.event_type == EventType.UNKNOWN

    def test_pull_request_opened(self):
        payload = {
            "action": "opened",
            "repository": {"full_name": "eandualem/agent-backbone"},
            "pull_request": {
                "number": 73,
                "title": "Add repo-local webhook routing",
                "state": "open",
                "html_url": "https://github.com/eandualem/agent-backbone/pull/73",
                "labels": [],
            },
        }
        event = normalize_event("pull_request", "opened", payload, "delivery-pr-1")
        assert event.event_type == EventType.PULL_REQUEST_OPENED
        assert event.issue.number == 73
        assert event.issue.is_pull_request is True
        assert event.issue.repo_full_name == "eandualem/agent-backbone"
