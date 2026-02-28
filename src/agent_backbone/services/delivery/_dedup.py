"""Shared notification deduplication.

Prevents the same issue+target pair from receiving duplicate notifications
within a time window. Used by both the gateway (opened/labeled/comment dedup)
and the lifecycle flow (close-then-next dedup).

The state is module-level and persists for the lifetime of the process.
This works because Phase 1 uses direct flow invocation — gateway and flows
run in the same Python process.
"""

from __future__ import annotations

import time

_recent_notifications: dict[tuple[int, str], float] = {}

DEFAULT_DEDUP_SECONDS = 10


def is_recent_notification(
    issue_number: int, target: str, dedup_seconds: int = DEFAULT_DEDUP_SECONDS
) -> bool:
    """Check if this issue+target was already notified recently.

    Returns True if a notification was sent within the dedup window (suppress).
    Returns False and records the notification if it's new (deliver).
    """
    key = (issue_number, target)
    now = time.monotonic()
    # Clean expired entries
    expired = [k for k, t in _recent_notifications.items() if now - t > dedup_seconds]
    for k in expired:
        del _recent_notifications[k]
    if key in _recent_notifications:
        return True
    _recent_notifications[key] = now
    return False


def clear() -> None:
    """Clear all dedup state. Useful for testing."""
    _recent_notifications.clear()
