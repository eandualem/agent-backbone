"""Short-window notification dedup.

GitHub sends several events for one action (``opened`` then ``labeled``,
webhook retries, poll overlap). This keeps a process-local record of
``(repo, issue, target)`` notifications so the same issue is not announced
to the same agent twice within ``routing.notification_dedup_seconds``.
"""

from __future__ import annotations

import time

_recent: dict[tuple[str, int, str], float] = {}

DEFAULT_DEDUP_SECONDS = 10


def is_recent_notification(
    repo: str, issue_number: int, target: str, dedup_seconds: int = DEFAULT_DEDUP_SECONDS
) -> bool:
    """True if this issue was announced to this target within the window.

    Otherwise records the notification and returns False.
    """
    now = time.monotonic()
    for stale in [k for k, t in _recent.items() if now - t > dedup_seconds]:
        del _recent[stale]
    key = (repo.casefold(), issue_number, target)
    if key in _recent:
        return True
    _recent[key] = now
    return False


def clear() -> None:
    """Forget every recorded notification (tests)."""
    _recent.clear()
