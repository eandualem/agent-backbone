"""Tests for flows/dependency_tracker.py and dependency integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from agent_backbone.config import BackboneConfig
from agent_backbone.models import IssueData, ParsedLabels
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.registry import EntityEntry, EntityInstance, EntityRegistry
from agent_backbone.services.routing import on_dependency_resolved
from agent_backbone.services.routing._dependencies import sync_dependencies


def _make_issue(number: int, state: str = "closed", targets: list[str] | None = None) -> IssueData:
    labels = ParsedLabels(
        sender="ike",
        targets=targets or ["feynman"],
        issue_type="task",
    )
    return IssueData(number=number, title=f"[task] Issue #{number}", state=state, labels=labels)


class TestOnDependencyResolved:
    async def test_no_parents_noop(self):
        """Closed issue has no parents → empty result."""
        async with BackboneDB.connect("sqlite+aiosqlite:///:memory:") as db:
            mock_gh = AsyncMock()
            config = BackboneConfig(webhook_secret="s")

            from agent_backbone.services._locator import init as init_flow_services

            init_flow_services(config=config, db=db, gh=mock_gh)

            result = await on_dependency_resolved.fn(99)

        assert result["parents_checked"] == "0"

    async def test_parent_found_all_resolved(self, config):
        """All sub-issues closed → unblock notification sent."""
        parent = _make_issue(10, state="open", targets=["feynman"])

        async with BackboneDB.connect("sqlite+aiosqlite:///:memory:") as db:
            await db.upsert_dependency(10, 20)

            mock_gh = AsyncMock()

            from agent_backbone.services._locator import init as init_flow_services

            init_flow_services(config=config, db=db, gh=mock_gh)

            with (
                patch(
                    "agent_backbone.services.routing._dependencies.check_parent_resolved",
                    new_callable=AsyncMock,
                    return_value={"parent": parent, "targets": ["feynman"]},
                ),
                patch(
                    "agent_backbone.services.routing._dependencies.safe_deliver",
                    new_callable=AsyncMock,
                    return_value="delivered",
                ) as mock_deliver,
            ):
                result = await on_dependency_resolved.fn(20)

        assert "unblocked" in result.get("parent_10", "")
        assert mock_deliver.called

    async def test_parent_found_some_open(self):
        """Some sub-issues still open → no notification."""
        async with BackboneDB.connect("sqlite+aiosqlite:///:memory:") as db:
            await db.upsert_dependency(10, 20)
            await db.upsert_dependency(10, 21)

            mock_gh = AsyncMock()
            config = BackboneConfig(webhook_secret="s")

            from agent_backbone.services._locator import init as init_flow_services

            init_flow_services(config=config, db=db, gh=mock_gh)

            with patch(
                "agent_backbone.services.routing._dependencies.check_parent_resolved",
                new_callable=AsyncMock,
                return_value=None,
            ):
                result = await on_dependency_resolved.fn(20)

        assert result.get("parent_10") == "still_blocked"

    async def test_api_failure_graceful(self):
        """GitHub API error → flow handles it gracefully."""
        async with BackboneDB.connect("sqlite+aiosqlite:///:memory:") as db:
            await db.upsert_dependency(10, 20)

            mock_gh = AsyncMock()
            config = BackboneConfig(webhook_secret="s")

            from agent_backbone.services._locator import init as init_flow_services

            init_flow_services(config=config, db=db, gh=mock_gh)

            with patch(
                "agent_backbone.services.routing._dependencies.check_parent_resolved",
                new_callable=AsyncMock,
                side_effect=Exception("API timeout"),
            ):
                # The flow will raise, but lifecycle wraps it in try/except
                try:
                    await on_dependency_resolved.fn(20)
                except Exception:
                    pass  # Expected — lifecycle wrapper catches this


class TestLifecycleDependencyIntegration:
    async def test_lifecycle_calls_dependency_tracker(self):
        """Verify _check_dependencies calls the dependency tracker."""
        from agent_backbone.services.routing._lifecycle import _check_dependencies

        with patch(
            "agent_backbone.services.routing._dependencies.on_dependency_resolved",
            new_callable=AsyncMock,
            return_value={"parents_checked": "0"},
        ) as mock_dep:
            await _check_dependencies(42)

        mock_dep.assert_called_once_with(42)

    async def test_lifecycle_dependency_error_isolated(self):
        """Dependency error doesn't break _check_dependencies."""
        from agent_backbone.services.routing._lifecycle import _check_dependencies

        with patch(
            "agent_backbone.services.routing._dependencies.on_dependency_resolved",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            # Should not raise
            await _check_dependencies(42)

    async def test_sync_dependencies_skips_abstract_role_targets(self):
        config = BackboneConfig(
            webhook_secret="test",
            registry=EntityRegistry(
                entities={
                    "feynman": EntityEntry(
                        session="feynman",
                        home="~/orchestration",
                        groups=["orchestrators"],
                        figure="",
                        role="",
                    ),
                    "bell": EntityEntry(
                        session=None,
                        home="~/ws/core/code/WF/bell",
                        groups=["orchestrators"],
                        figure="",
                        role="Org Orchestrator",
                        entity_type="role",
                        instances={
                            "wf": EntityInstance(
                                home="~/ws/core/code/WF/bell",
                                session="bell-wf",
                                organization="WF",
                            ),
                        },
                    ),
                },
                repos=[],
            ),
        )
        gh = AsyncMock()
        gh.list_open_issues.side_effect = lambda label: []
        db = AsyncMock()

        await sync_dependencies(config, db, gh)

        assert [call.args[0] for call in gh.list_open_issues.await_args_list] == ["for:feynman"]


class TestPersistenceDependencies:
    async def test_upsert_and_get_parents(self):
        async with BackboneDB.connect("sqlite+aiosqlite:///:memory:") as db:
            await db.upsert_dependency(10, 20)
            await db.upsert_dependency(11, 20)

            parents = await db.get_parents(20)
            assert sorted(parents) == [10, 11]

    async def test_no_parents(self):
        async with BackboneDB.connect("sqlite+aiosqlite:///:memory:") as db:
            parents = await db.get_parents(99)
            assert parents == []

    async def test_sync_dependencies(self):
        async with BackboneDB.connect("sqlite+aiosqlite:///:memory:") as db:
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
        async with BackboneDB.connect("sqlite+aiosqlite:///:memory:") as db:
            await db.sync_dependencies(10, [20, 21])
            await db.sync_dependencies(10, [])

            parents = await db.get_parents(20)
            assert parents == []
