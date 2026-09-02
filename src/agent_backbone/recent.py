"""``RecentKeys`` — a process-local "seen within the last N seconds" set.

Several parts of the backbone must not repeat themselves within a window:
announcing an issue to an agent, escalating the same stall, alerting about
the same stuck pane, hinting the same Telegram topic. Each keeps one
instance of this; the keys are theirs, the eviction is shared.
"""

from __future__ import annotations

import time
from collections.abc import Hashable


class RecentKeys:
    """Keys marked less than ``ttl_seconds`` ago. Monotonic clock; not thread-safe."""

    def __init__(self, ttl_seconds: float) -> None:
        self.ttl_seconds = ttl_seconds
        self._marked: dict[Hashable, float] = {}

    def _evict(self, now: float, ttl: float) -> None:
        for key in [k for k, t in self._marked.items() if now - t > ttl]:
            del self._marked[key]

    def seen(self, key: Hashable, *, ttl_seconds: float | None = None) -> bool:
        """Whether ``key`` was marked within the window (does not mark it).

        ``ttl_seconds`` changes the window for this call only (a setting the
        caller reads at run time); entries are evicted on the longer of the
        two windows so a wider per-call window never loses its history.
        """
        now = time.monotonic()
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        self._evict(now, max(ttl, self.ttl_seconds))
        marked_at = self._marked.get(key)
        return marked_at is not None and now - marked_at <= ttl

    def mark(self, key: Hashable) -> None:
        self._marked[key] = time.monotonic()

    def check_and_mark(self, key: Hashable, *, ttl_seconds: float | None = None) -> bool:
        """True when ``key`` is recent; otherwise marks it now and returns False."""
        if self.seen(key, ttl_seconds=ttl_seconds):
            return True
        self.mark(key)
        return False

    def forget(self, key: Hashable) -> None:
        self._marked.pop(key, None)

    def retain(self, keys: set[Hashable]) -> None:
        """Drop every key not in ``keys``."""
        for key in [k for k in self._marked if k not in keys]:
            del self._marked[key]

    def clear(self) -> None:
        self._marked.clear()
