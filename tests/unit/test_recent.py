"""Tests for the shared time-windowed dedup helper."""

from __future__ import annotations

from unittest.mock import patch

from agent_backbone.recent import RecentKeys


class TestRecentKeys:
    def test_check_and_mark_reports_the_second_sighting(self):
        recent = RecentKeys(10)
        assert recent.check_and_mark("a") is False
        assert recent.check_and_mark("a") is True
        assert recent.check_and_mark("b") is False

    def test_entries_expire(self):
        recent = RecentKeys(10)
        with patch("agent_backbone.recent.time.monotonic", return_value=100.0):
            recent.mark("a")
        with patch("agent_backbone.recent.time.monotonic", return_value=105.0):
            assert recent.seen("a") is True
        with patch("agent_backbone.recent.time.monotonic", return_value=111.0):
            assert recent.seen("a") is False

    def test_per_call_ttl_overrides_the_default(self):
        recent = RecentKeys(10)
        with patch("agent_backbone.recent.time.monotonic", return_value=100.0):
            recent.mark("a")
        with patch("agent_backbone.recent.time.monotonic", return_value=103.0):
            assert recent.seen("a", ttl_seconds=2) is False
            assert recent.seen("a") is True

    def test_forget_retain_and_clear(self):
        recent = RecentKeys(10)
        for key in ("a", "b", "c"):
            recent.mark(key)
        recent.forget("a")
        assert recent.seen("a") is False
        recent.retain({"b"})
        assert recent.seen("c") is False
        assert recent.seen("b") is True
        recent.clear()
        assert recent.seen("b") is False


def test_a_wider_window_keeps_marks_a_narrower_lookup_would_evict():
    """Callers pass the window from settings; a later wider lookup must still see the mark."""
    recent = RecentKeys(10)
    with patch("agent_backbone.recent.time.monotonic", return_value=100.0):
        recent.mark("a")
        recent.seen("a", ttl_seconds=3600)  # widens retention for the instance
    with patch("agent_backbone.recent.time.monotonic", return_value=120.0):
        assert recent.seen("a") is False  # outside the default window …
        assert recent.seen("a", ttl_seconds=3600) is True  # … but not evicted
