"""One complete queue read per repository and fresh data on the next tick."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, create_autospec, patch

from agent_backbone.config import AgentsConfig, AgentSpec
from agent_backbone.models import IssueData, ParsedLabels
from agent_backbone.recent import RecentKeys
from agent_backbone.services.github import GitHubClient, QueueSnapshot
from agent_backbone.services.routing import list_open_queue_for_target


async def test_concurrent_filtered_lists_share_complete_repo_request():
    gh = AsyncMock()
    issue = IssueData(number=1, repo_full_name="acme/app", labels=ParsedLabels(targets=["a"]))
    gh.list_issues.return_value = [issue]
    snapshot = QueueSnapshot(gh)
    a, b = await asyncio.gather(
        snapshot.list_issues(repo_full_name="acme/app", labels=["for:a"]),
        snapshot.list_issues(repo_full_name="ACME/app", labels=["for:b"]),
    )
    assert a == [issue] and b == []
    gh.list_issues.assert_awaited_once_with(state="open", repo_full_name="acme/app", all_pages=True)
    gh.list_issues.return_value = []
    assert await QueueSnapshot(gh).list_issues(repo_full_name="acme/app") == []


async def test_queue_orders_real_cross_repo_age_and_recorded_dependents(config, db):
    agent = AgentSpec(name="a", dir="/work", repo="acme/new", watches=("acme/old",))
    config = replace(config, agents=AgentsConfig(specs={"a": agent}))
    young = IssueData(
        number=1,
        repo_full_name="acme/new",
        created_at="2026-09-05T00:00:00Z",
        labels=ParsedLabels(targets=["a"], issue_type="task"),
    )
    old = IssueData(
        number=9000,
        repo_full_name="acme/old",
        created_at="2026-08-01T00:00:00Z",
        labels=ParsedLabels(targets=["a"], issue_type="task"),
    )
    gh = AsyncMock()
    gh.list_issues.side_effect = lambda **kw: (
        [young] if kw["repo_full_name"] == "acme/new" else [old]
    )
    snapshot = QueueSnapshot(gh)
    assert await list_open_queue_for_target(config, "a", snapshot, db=db) == [old, young]
    await db.dependencies.sync(2, [1], repo="acme/new")
    assert await list_open_queue_for_target(config, "a", snapshot, db=db) == [young, old]
    # Matching issue numbers in another repo must not receive the dependent bonus.
    assert (await db.dependencies.counts()).get(("acme/old", 1), 0) == 0


def test_refreshing_old_key_keeps_ordered_expiry_correct():
    cache = RecentKeys(10)
    with patch("agent_backbone.recent.time.monotonic", return_value=0):
        cache.mark("old")
    with patch("agent_backbone.recent.time.monotonic", return_value=5):
        cache.mark("middle")
    with patch("agent_backbone.recent.time.monotonic", return_value=9):
        cache.mark("old")
    with patch("agent_backbone.recent.time.monotonic", return_value=16):
        assert cache.seen("old")
        assert not cache.seen("middle")
        assert len(cache._marked) == 1


async def test_arbitrary_labels_do_not_fetch_a_complete_queue_first():
    gh = create_autospec(GitHubClient, instance=True)
    expected = [IssueData(number=1, repo_full_name="acme/app")]

    async def fetch(**kwargs):
        assert kwargs == {
            "state": "open",
            "repo_full_name": "acme/app",
            "labels": ["bug"],
            "per_page": 2,
        }
        return expected

    gh.list_issues.side_effect = fetch
    assert (
        await QueueSnapshot(gh).list_issues(repo_full_name="acme/app", labels=["bug"], per_page=2)
        == expected
    )
    gh.list_issues.assert_awaited_once()
