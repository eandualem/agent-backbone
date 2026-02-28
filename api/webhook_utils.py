"""Webhook utility functions — signature verification and event normalization.

Extracted from gateway/server.py for use by FastAPI webhook routes.
These are pure functions with no module-level state.
"""

from __future__ import annotations

import hashlib
import hmac

from agent_backbone.models import IssueEvent


def verify_signature(payload_body: bytes, signature_header: str | None, secret: str) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature."""
    if not signature_header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), payload_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def normalize_event(
    event_type_str: str, action: str, payload: dict, delivery_id: str
) -> IssueEvent:
    """Normalize a raw GitHub webhook into an IssueEvent."""
    return IssueEvent.from_webhook(event_type_str, action, payload, delivery_id)
