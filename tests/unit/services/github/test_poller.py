"""Tests for the GitHub polling connector (poll intake + startup backfill)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_backbone.config import AgentsConfig, AgentSpec
from agent_backbone.models import EventType
from agent_backbone.services.github._poller import (
    GitHubPoller,
    comment_event_from_api,
    issue_event_from_api,
)
from tests.conftest import TEST_REPO, make_agents, make_config


def _issue(number: int, *, state="open", created="2026-08-31T10:00:00Z", updated=None, labels=()):
    return {
        "number": number,
        "title": f"Issue {number}",
        "state": state,
        "created_at": created,
        "updated_at": updated or created,
        "html_url": f"https://github.com/{TEST_REPO}/issues/{number}",
        "labels": [{"name": name} for name in labels],
    }


def _comment(cid: int, issue_number: int, body: str, updated="2026-08-31T10:05:00Z"):
    return {
        "id": cid,
        "body": body,
        "user": {"login": "someone"},
        "issue_url": f"https://api.github.com/repos/{TEST_REPO}/issues/{issue_number}",
        "created_at": updated,
        "updated_at": updated,
    }


class TestEventConversion:
    def test_new_issue_becomes_opened(self):
        event = issue_event_from_api(
            _issue(1, labels=["for:ike"]), TEST_REPO, "2026-08-31T09:00:00Z"
        )
        assert event.event_type == EventType.ISSUE_OPENED
        assert event.issue.labels.targets == ["ike"]
        assert event.issue.repo_full_name == TEST_REPO
        assert event.delivery_id == f"poll:{TEST_REPO}#1@2026-08-31T10:00:00Z"

    def test_old_issue_updated_becomes_labeled(self):
        item = _issue(1, created="2026-08-30T00:00:00Z", updated="2026-08-31T10:00:00Z")
        event = issue_event_from_api(item, TEST_REPO, "2026-08-31T09:00:00Z")
        assert event.event_type == EventType.ISSUE_LABELED

    def test_closed_issue(self):
        event = issue_event_from_api(_issue(1, state="closed"), TEST_REPO, "2026-08-31T09:00:00Z")
        assert event.event_type == EventType.ISSUE_CLOSED

    def test_pull_requests_are_skipped(self):
        item = {**_issue(1), "pull_request": {"url": "x"}}
        assert issue_event_from_api(item, TEST_REPO, "2026-08-31T09:00:00Z") is None

    def test_comment_event(self):
        event = comment_event_from_api(_comment(9, 1, "[from:ike] ack"), _issue(1), TEST_REPO)
        assert event.event_type == EventType.COMMENT_CREATED
        assert event.comment.body == "[from:ike] ack"
        assert event.issue.number == 1
        assert event.delivery_id == "poll:comment:9"


class TestPolledRepos:
    def test_every_owned_and_watched_repo_once(self, tmp_path):
        config = make_config(
            tmp_path,
            agents=AgentsConfig(
                specs={
                    "a": AgentSpec(name="a", dir="/x", repo="acme/app"),
                    "b": AgentSpec(name="b", dir="/y", repo="acme/app", watches=(TEST_REPO,)),
                    "c": AgentSpec(name="c", dir="/z", repo=TEST_REPO),
                }
            ),
        )
        assert config.agents.repos == ["acme/app", TEST_REPO]


@pytest.fixture
def services():
    delivery = MagicMock()
    delivery.is_recent_notification = MagicMock(return_value=False)
    dispatch = MagicMock()
    dispatch.issue_dispatcher = AsyncMock(
        return_value=MagicMock(delivered=["ike"], offline=[], deferred=[])
    )
    dispatch.on_issue_closed = AsyncMock(return_value={"ike": "queue_empty"})
    return delivery, dispatch


@pytest.fixture
def config(tmp_path):
    """One agent owning the shared repo, so exactly one repository is polled."""
    return make_config(
        tmp_path, agents=make_agents(tmp_path, names=("ike",), shared_repo="")
    ).__class__(
        **{
            **make_config(tmp_path).__dict__,
            "agents": AgentsConfig(
                specs={"ike": AgentSpec(name="ike", dir=str(tmp_path / "ike"), repo=TEST_REPO)}
            ),
        }
    )


class TestGitHubPoller:
    async def test_dispatches_new_issue_and_comment_once(self, config, db, services):
        delivery, dispatch = services
        gh = AsyncMock()
        gh.list_issues_since = AsyncMock(return_value=[_issue(1, labels=["for:ike"])])
        gh.list_comments_since = AsyncMock(return_value=[_comment(9, 1, "[from:leo] hi")])
        poller = GitHubPoller(lambda: config, db, gh, delivery, dispatch)
        poller._since[TEST_REPO] = "2026-08-31T09:00:00Z"

        first = await poller.run()
        second = await poller.run()

        assert first == {"dispatch": 2}
        assert dispatch.issue_dispatcher.await_count == 2
        kinds = [c.args[0].event_type for c in dispatch.issue_dispatcher.await_args_list]
        assert kinds == [EventType.ISSUE_OPENED, EventType.COMMENT_CREATED]
        assert second == {"deduped": 2}
        assert dispatch.issue_dispatcher.await_count == 2
        assert gh.list_issues_since.await_args_list[1].args[1] == "2026-08-31T10:05:01Z"
        # Both events were stored in the activity feed
        assert len(await db.query_events(repo=TEST_REPO)) == 2

    async def test_closed_issue_goes_to_lifecycle(self, config, db, services):
        delivery, dispatch = services
        gh = AsyncMock()
        gh.list_issues_since = AsyncMock(return_value=[_issue(4, state="closed")])
        gh.list_comments_since = AsyncMock(return_value=[])
        poller = GitHubPoller(config, db, gh, delivery, dispatch)

        summary = await poller.run()

        assert summary == {"lifecycle": 1}
        dispatch.on_issue_closed.assert_awaited_once()

    async def test_comment_on_unlisted_issue_fetches_it(self, config, db, services):
        delivery, dispatch = services
        gh = AsyncMock()
        gh.list_issues_since = AsyncMock(return_value=[])
        gh.list_comments_since = AsyncMock(return_value=[_comment(9, 7, "hello")])
        gh.get_issue_raw = AsyncMock(return_value=_issue(7, labels=["for:ike"]))
        poller = GitHubPoller(config, db, gh, delivery, dispatch)

        await poller.run()

        gh.get_issue_raw.assert_awaited_once_with(7, TEST_REPO)
        event = dispatch.issue_dispatcher.await_args.args[0]
        assert event.issue.number == 7 and event.comment.body == "hello"

    async def test_api_failure_is_nonfatal(self, config, db, services):
        delivery, dispatch = services
        gh = AsyncMock()
        gh.list_issues_since = AsyncMock(side_effect=RuntimeError("rate limited"))
        poller = GitHubPoller(config, db, gh, delivery, dispatch)
        assert await poller.run() == {}

    async def test_dedup_survives_restart_via_events_table(self, config, db, services):
        delivery, dispatch = services
        gh = AsyncMock()
        gh.list_issues_since = AsyncMock(return_value=[_issue(1, labels=["for:ike"])])
        gh.list_comments_since = AsyncMock(return_value=[])
        first = GitHubPoller(config, db, gh, delivery, dispatch)
        first._since[TEST_REPO] = "2026-08-31T09:00:00Z"
        await first.run()
        db._seen_deliveries.clear()  # restart: hot cache gone, DB remains

        second = GitHubPoller(config, db, gh, delivery, dispatch)
        second._since[TEST_REPO] = "2026-08-31T09:00:00Z"
        summary = await second.run()

        assert summary == {"deduped": 1}
        assert dispatch.issue_dispatcher.await_count == 1

    async def test_backfill_resumes_from_last_stored_event(self, config, db, services):
        delivery, dispatch = services
        await db.record_event(
            delivery_id="x", source="webhook", event_type="issue_opened", repo=TEST_REPO
        )
        gh = AsyncMock()
        gh.list_issues_since = AsyncMock(return_value=[])
        gh.list_comments_since = AsyncMock(return_value=[])
        await GitHubPoller(config, db, gh, delivery, dispatch).run()
        since = gh.list_issues_since.await_args.args[1]
        # A little before the stored event, never the full lookback window
        assert since.endswith("Z") and since >= "2020"
