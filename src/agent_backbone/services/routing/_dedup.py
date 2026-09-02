"""Short-window notification dedup.

GitHub sends several events for one action (``opened`` then ``labeled``,
webhook retries, poll overlap). A process-local record of
``(repo, issue, target)`` announcements keeps the same issue from being
announced to the same agent twice within ``routing.notification_dedup_seconds``.
"""

from __future__ import annotations

from agent_backbone.recent import RecentKeys

DEFAULT_DEDUP_SECONDS = 10

_recent = RecentKeys(DEFAULT_DEDUP_SECONDS)


def is_recent_notification(
    repo: str, issue_number: int, target: str, dedup_seconds: int = DEFAULT_DEDUP_SECONDS
) -> bool:
    """True if this issue was announced to this target within the window.

    Otherwise records the announcement and returns False.
    """
    return _recent.check_and_mark(
        (repo.casefold(), issue_number, target), ttl_seconds=dedup_seconds
    )


def clear() -> None:
    """Forget every recorded notification (tests)."""
    _recent.clear()
