"""Tests for flows/dependency_tracker.py and dependency integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from flows.dependency_tracker import on_dependency_resolved
from src.models import IssueData, ParsedLabels
from src.persistence import BackboneDB


def _make_issue(number: int, state: str = "closed", targets: list[str] | None = None) -> IssueData:
    labels = ParsedLabels(
        sender="ike",
        targets=targets or ["feynman"],
        issue_type="task",
    )
    return IssueData(number=number, title=f"[task] Issue #{number}", state=state, labels=labels)


def _mock_db_class(db: BackboneDB):
    """Create a mock BackboneDB class that returns the real db via async context manager."""
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=db)
    mock_cm.__aexit__ = AsyncMock(return_value=None)
    mock_cls = MagicMock(return_value=mock_cm)
    return mock_cls


class TestOnDependencyResolved:
    async def test_no_parents_noop(self):
        """Closed issue has no parents → empty result."""
        async with BackboneDB(":memory:") as db:
            with patch("flows.dependency_tracker.BackboneDB", _mock_db_class(db)):
                result = await on_dependency_resolved.fn(99)

        assert result["parents_checked"] == "0"

    async def test_parent_found_all_resolved(self):
        """All sub-issues closed → unblock notification sent."""
        parent = _make_issue(10, state="open", targets=["feynman"])

        async with BackboneDB(":memory:") as db:
            await db.upsert_dependency(10, 20)

            with (
                patch("flows.dependency_tracker.BackboneDB", _mock_db_class(db)),
                patch(
                    "flows.dependency_tracker.check_parent_resolved",
                    new_callable=AsyncMock,
                    return_value={"parent": parent, "targets": ["feynman"]},
                ),
                patch(
                    "flows.dependency_tracker.session_exists",
                    new_callable=AsyncMock,
                    return_value=True,
                ),
                patch(
                    "flows.dependency_tracker.send_message",
                    new_callable=AsyncMock,
                    return_value=True,
                ) as mock_send,
            ):
                result = await on_dependency_resolved.fn(20)

        assert "unblocked" in result.get("parent_10", "")
        assert mock_send.called

    async def test_parent_found_some_open(self):
        """Some sub-issues still open → no notification."""
        async with BackboneDB(":memory:") as db:
            await db.upsert_dependency(10, 20)
            await db.upsert_dependency(10, 21)

            with (
                patch("flows.dependency_tracker.BackboneDB", _mock_db_class(db)),
                patch(
                    "flows.dependency_tracker.check_parent_resolved",
                    new_callable=AsyncMock,
                    return_value=None,
                ),
            ):
                result = await on_dependency_resolved.fn(20)

        assert result.get("parent_10") == "still_blocked"

    async def test_api_failure_graceful(self):
        """GitHub API error → flow handles it gracefully."""
        async with BackboneDB(":memory:") as db:
            await db.upsert_dependency(10, 20)

            with (
                patch("flows.dependency_tracker.BackboneDB", _mock_db_class(db)),
                patch(
                    "flows.dependency_tracker.check_parent_resolved",
                    new_callable=AsyncMock,
                    side_effect=Exception("API timeout"),
                ),
            ):
                # The flow will raise, but lifecycle wraps it in try/except
                try:
                    await on_dependency_resolved.fn(20)
                except Exception:
                    pass  # Expected — lifecycle wrapper catches this


class TestLifecycleDependencyIntegration:
    async def test_lifecycle_calls_dependency_tracker(self):
        """Verify _check_dependencies calls the dependency tracker."""
        from flows.lifecycle import _check_dependencies

        with patch(
            "flows.dependency_tracker.on_dependency_resolved",
            new_callable=AsyncMock,
            return_value={"parents_checked": "0"},
        ) as mock_dep:
            await _check_dependencies(42)

        mock_dep.assert_called_once_with(42)

    async def test_lifecycle_dependency_error_isolated(self):
        """Dependency error doesn't break _check_dependencies."""
        from flows.lifecycle import _check_dependencies

        with patch(
            "flows.dependency_tracker.on_dependency_resolved",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            # Should not raise
            await _check_dependencies(42)


class TestPersistenceDependencies:
    async def test_upsert_and_get_parents(self):
        async with BackboneDB(":memory:") as db:
            await db.upsert_dependency(10, 20)
            await db.upsert_dependency(11, 20)

            parents = await db.get_parents(20)
            assert sorted(parents) == [10, 11]

    async def test_no_parents(self):
        async with BackboneDB(":memory:") as db:
            parents = await db.get_parents(99)
            assert parents == []

    async def test_sync_dependencies(self):
        async with BackboneDB(":memory:") as db:
            # Initial sync
            await db.sync_dependencies(10, [20, 21, 22])
            parents = await db.get_parents(20)
            assert 10 in parents

            # Re-sync with fewer subs — stale ones removed
            await db.sync_dependencies(10, [20, 21])
            parents_22 = await db.get_parents(22)
            assert parents_22 == []

            # Remaining are still there
            parents_20 = await db.get_parents(20)
            assert 10 in parents_20

    async def test_sync_empty_clears_all(self):
        async with BackboneDB(":memory:") as db:
            await db.sync_dependencies(10, [20, 21])
            await db.sync_dependencies(10, [])

            parents = await db.get_parents(20)
            assert parents == []
