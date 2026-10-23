"""Tests for routing/_dependencies.py and dependency integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from agent_backbone.models import DeliveryOutcome, IssueData, ParsedLabels
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.routing import DeliveryReport
from agent_backbone.services.routing._dependencies import on_dependency_resolved, sync_dependencies

_DEP = "agent_backbone.services.routing._dependencies"


def _make_issue(number: int, state: str = "closed", targets: list[str] | None = None) -> IssueData:
    labels = ParsedLabels(sender="ike", targets=targets or ["feynman"], issue_type="task")
    return IssueData(
        number=number,
        repo_full_name="acme/app",
        title=f"[task] Issue #{number}",
        state=state,
        labels=labels,
    )


def _gh(parent: int | None = None) -> AsyncMock:
    gh = AsyncMock()
    gh.get_parent_number = AsyncMock(return_value=parent)
    return gh


class TestOnDependencyResolved:
    async def test_no_parents_noop(self, config):
        async with BackboneDB.connect() as db:
            result = await on_dependency_resolved(99, "", config, db, _gh())
        assert result["parents_checked"] == "0"

    async def test_link_made_since_the_last_sync_still_unblocks_the_parent(self, config):
        # Nothing recorded yet: the parent comes from GitHub at close.
        gh = _gh(parent=10)
        gh.get_sub_issues = AsyncMock(return_value=[_make_issue(20)])
        gh.get_issue = AsyncMock(return_value=_make_issue(10, state="open", targets=["brunel"]))
        async with BackboneDB.connect() as db:
            with patch(
                f"{_DEP}.safe_deliver",
                new_callable=AsyncMock,
                return_value=DeliveryReport(DeliveryOutcome.DELIVERED),
            ):
                result = await on_dependency_resolved(20, "acme/app", config, db, gh)

        gh.get_parent_number.assert_awaited_once_with(20, repo_full_name="acme/app")
        assert result["parent_10"] == "unblocked_delivered_to:brunel"

    async def test_parent_found_all_resolved(self, config):
        parent = _make_issue(10, state="open", targets=["feynman"])
        async with BackboneDB.connect() as db:
            await db.dependencies.sync(10, [20])
            with (
                patch(
                    f"{_DEP}.check_parent_resolved",
                    new_callable=AsyncMock,
                    return_value={"parent": parent, "targets": ["feynman"]},
                ),
                patch(
                    f"{_DEP}.safe_deliver",
                    new_callable=AsyncMock,
                    return_value=DeliveryReport(DeliveryOutcome.DELIVERED),
                ) as d,
            ):
                result = await on_dependency_resolved(20, "", config, db, _gh(parent=10))

        assert result["parents_checked"] == "1"
        assert result["parent_10"] == "unblocked_delivered_to:feynman"
        assert d.await_args.args[0] == "feynman"

    async def test_parent_found_some_open(self, config):
        async with BackboneDB.connect() as db:
            await db.dependencies.sync(10, [20, 21])
            with patch(f"{_DEP}.check_parent_resolved", new_callable=AsyncMock, return_value=None):
                result = await on_dependency_resolved(20, "", config, db, _gh())
        assert result["parent_10"] == "still_blocked"

    async def test_unknown_target_not_delivered(self, config):
        parent = _make_issue(10, state="open", targets=["nobody"])
        async with BackboneDB.connect() as db:
            await db.dependencies.sync(10, [20])
            with (
                patch(
                    f"{_DEP}.check_parent_resolved",
                    new_callable=AsyncMock,
                    return_value={"parent": parent, "targets": ["nobody"]},
                ),
                patch(f"{_DEP}.safe_deliver", new_callable=AsyncMock) as d,
            ):
                result = await on_dependency_resolved(20, "", config, db, _gh())
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

    async def test_closed_parent_is_not_unblocked(self, config):
        from agent_backbone.services.routing._dependencies import check_parent_resolved

        gh = AsyncMock()
        gh.get_sub_issues = AsyncMock(return_value=[_make_issue(20)])
        gh.get_issue = AsyncMock(return_value=_make_issue(10, state="closed"))
        assert await check_parent_resolved(config, 10, gh) is None


class TestLifecycleDependencyIntegration:
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
            await db.dependencies.sync(10, [20])
            await db.dependencies.sync(11, [20])
            assert sorted(await db.dependencies.parents(20)) == [10, 11]

    async def test_no_parents(self):
        async with BackboneDB.connect() as db:
            assert await db.dependencies.parents(99) == []

    async def test_sync_dependencies(self):
        async with BackboneDB.connect() as db:
            await db.dependencies.sync(10, [20, 21, 22])
            assert 10 in await db.dependencies.parents(20)
            await db.dependencies.sync(10, [20, 21])
            assert await db.dependencies.parents(22) == []
            assert 10 in await db.dependencies.parents(20)

    async def test_sync_empty_clears_all(self):
        async with BackboneDB.connect() as db:
            await db.dependencies.sync(10, [20, 21])
            await db.dependencies.sync(10, [])
            assert await db.dependencies.parents(20) == []

    async def test_a_whole_sync_is_one_transaction(self):
        async with BackboneDB.connect() as db:
            await db.dependencies.sync(10, [20], repo="acme/app")
            await db.dependencies.sync(11, [21], repo="acme/app")
            with patch.object(db.dependencies, "_tx", wraps=db.dependencies._tx) as tx:
                await db.dependencies.apply_sync(
                    {"acme/app": {10, 12}}, {("acme/app", 10): [20], ("acme/app", 12): [22]}
                )
            assert tx.call_count == 1  # a monitor pass never reads half of it
            assert await db.dependencies.parents(20, repo="acme/app") == [10]
            assert await db.dependencies.parents(21, repo="acme/app") == []  # 11 is closed
            assert await db.dependencies.parents(22, repo="acme/app") == [12]


class TestSubIssueAnswers:
    async def test_empty_answer_syncs_but_a_failed_fetch_does_not(self, config):
        # S1-16: an empty list clears stale edges; a failed fetch keeps them.
        gh = AsyncMock()
        gh.list_issues = AsyncMock(return_value=[_make_issue(10, "open")])
        gh.get_sub_issues = AsyncMock(return_value=[])
        db = AsyncMock()
        await sync_dependencies(config, db, gh)
        assert db.dependencies.apply_sync.await_args.args[1] == {("acme/app", 10): []}

        gh.get_sub_issues = AsyncMock(return_value=None)
        db = AsyncMock()
        await sync_dependencies(config, db, gh)
        assert db.dependencies.apply_sync.await_args.args[1] == {}

    async def test_no_request_when_the_listing_counts_no_sub_issues(self, config):
        listed = _make_issue(10, "open").model_copy(update={"sub_issues_total": 0})
        gh = AsyncMock()
        gh.list_issues = AsyncMock(return_value=[listed])
        db = AsyncMock()
        await sync_dependencies(config, db, gh)
        gh.get_sub_issues.assert_not_awaited()
        assert db.dependencies.apply_sync.await_args.args[1] == {("acme/app", 10): []}
