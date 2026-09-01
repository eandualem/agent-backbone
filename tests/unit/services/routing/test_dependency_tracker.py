"""Tests for routing/_dependencies.py and dependency integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from agent_backbone.models import IssueData, ParsedLabels
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.routing._dependencies import on_dependency_resolved, sync_dependencies
from agent_backbone.services.routing._lifecycle import _check_dependencies

_DEP = "agent_backbone.services.routing._dependencies"


def _make_issue(number: int, state: str = "closed", targets: list[str] | None = None) -> IssueData:
    labels = ParsedLabels(sender="ike", targets=targets or ["feynman"], issue_type="task")
    return IssueData(number=number, title=f"[task] Issue #{number}", state=state, labels=labels)


class TestOnDependencyResolved:
    async def test_no_parents_noop(self, config):
        async with BackboneDB.connect() as db:
            result = await on_dependency_resolved(99, "", config, db, AsyncMock())
        assert result["parents_checked"] == "0"

    async def test_parent_found_all_resolved(self, config):
        parent = _make_issue(10, state="open", targets=["feynman"])
        async with BackboneDB.connect() as db:
            await db.sync_dependencies(10, [20])
            with (
                patch(
                    f"{_DEP}.check_parent_resolved",
                    new_callable=AsyncMock,
                    return_value={"parent": parent, "targets": ["feynman"]},
                ),
                patch(
                    f"{_DEP}.safe_deliver", new_callable=AsyncMock, return_value="delivered"
                ) as d,
            ):
                result = await on_dependency_resolved(20, "", config, db, AsyncMock())

        assert result["parent_10"] == "unblocked_delivered_to:feynman"
        assert d.await_args.args[0] == "feynman"

    async def test_parent_found_some_open(self, config):
        async with BackboneDB.connect() as db:
            await db.sync_dependencies(10, [20, 21])
            with patch(f"{_DEP}.check_parent_resolved", new_callable=AsyncMock, return_value=None):
                result = await on_dependency_resolved(20, "", config, db, AsyncMock())
        assert result["parent_10"] == "still_blocked"

    async def test_unknown_target_not_delivered(self, config):
        parent = _make_issue(10, state="open", targets=["nobody"])
        async with BackboneDB.connect() as db:
            await db.sync_dependencies(10, [20])
            with (
                patch(
                    f"{_DEP}.check_parent_resolved",
                    new_callable=AsyncMock,
                    return_value={"parent": parent, "targets": ["nobody"]},
                ),
                patch(f"{_DEP}.safe_deliver", new_callable=AsyncMock) as d,
            ):
                result = await on_dependency_resolved(20, "", config, db, AsyncMock())
        assert result["parent_10"] == "unblocked_no_delivery"
        d.assert_not_called()


class TestCheckParentResolved:
    async def test_all_closed(self, config):
        from agent_backbone.services.routing._dependencies import check_parent_resolved

        gh = AsyncMock()
        gh.get_sub_issues = AsyncMock(return_value=[_make_issue(20), _make_issue(21)])
        gh.get_issue = AsyncMock(return_value=_make_issue(10, state="open", targets=["ike"]))
        result = await check_parent_resolved(config, 10, gh)
        assert result == {"parent": gh.get_issue.return_value, "targets": ["ike"]}

    async def test_some_open(self, config):
        from agent_backbone.services.routing._dependencies import check_parent_resolved

        gh = AsyncMock()
        gh.get_sub_issues = AsyncMock(return_value=[_make_issue(20), _make_issue(21, "open")])
        assert await check_parent_resolved(config, 10, gh) is None


class TestLifecycleDependencyIntegration:
    async def test_lifecycle_calls_dependency_tracker(self, config):
        db, gh = AsyncMock(), AsyncMock()
        with patch(
            f"{_DEP}.on_dependency_resolved",
            new_callable=AsyncMock,
            return_value={"parents_checked": "0"},
        ) as mock_dep:
            await _check_dependencies(42, "acme/app", config, db, gh)
        mock_dep.assert_called_once_with(42, "acme/app", config, db, gh)

    async def test_lifecycle_dependency_error_isolated(self, config):
        with patch(
            f"{_DEP}.on_dependency_resolved",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            await _check_dependencies(42, "acme/app", config, AsyncMock(), AsyncMock())

    async def test_sync_dependencies_queries_each_agent(self, config):
        gh = AsyncMock()
        gh.list_issues = AsyncMock(return_value=[])
        await sync_dependencies(config, AsyncMock(), gh)
        calls = gh.list_issues.await_args_list
        queried = {
            (call.kwargs["repo_full_name"], call.kwargs["labels"][0])
            for call in calls
            if call.kwargs.get("labels")
        }
        assert queried == {
            (repo, f"for:{spec.name}") for spec in config.agents for repo in spec.repos
        }

    async def test_sync_dependencies_skipped_without_github(self, config, tmp_path):
        await sync_dependencies(config, AsyncMock(), None)


class TestPersistenceDependencies:
    async def test_upsert_and_get_parents(self):
        async with BackboneDB.connect() as db:
            await db.sync_dependencies(10, [20])
            await db.sync_dependencies(11, [20])
            assert sorted(await db.get_parents(20)) == [10, 11]

    async def test_no_parents(self):
        async with BackboneDB.connect() as db:
            assert await db.get_parents(99) == []

    async def test_sync_dependencies(self):
        async with BackboneDB.connect() as db:
            await db.sync_dependencies(10, [20, 21, 22])
            assert 10 in await db.get_parents(20)
            await db.sync_dependencies(10, [20, 21])
            assert await db.get_parents(22) == []
            assert 10 in await db.get_parents(20)

    async def test_sync_empty_clears_all(self):
        async with BackboneDB.connect() as db:
            await db.sync_dependencies(10, [20, 21])
            await db.sync_dependencies(10, [])
            assert await db.get_parents(20) == []
