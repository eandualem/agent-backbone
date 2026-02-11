"""Tests for gateway/server.py."""

from __future__ import annotations

import hashlib
import hmac
import json

from gateway.server import (
    is_duplicate,
    is_recent_notification,
    normalize_event,
    verify_signature,
    _seen_deliveries,
    _recent_notifications,
)
from src.models import EventType


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
    def setup_method(self):
        _seen_deliveries.clear()

    def test_first_delivery(self):
        assert is_duplicate("abc-123") is False

    def test_duplicate_delivery(self):
        is_duplicate("abc-123")
        assert is_duplicate("abc-123") is True

    def test_empty_delivery_id(self):
        assert is_duplicate("") is False

    def test_max_capacity(self):
        for i in range(150):
            is_duplicate(f"delivery-{i}", max_ids=100)
        # Oldest entries should be evicted
        assert is_duplicate("delivery-0", max_ids=100) is False  # Evicted, counts as new
        assert is_duplicate("delivery-149", max_ids=100) is True  # Still present


class TestIsRecentNotification:
    def setup_method(self):
        _recent_notifications.clear()

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
