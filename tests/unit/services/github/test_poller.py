"""Tests for the GitHub polling connector."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_backbone.config import AgentsConfig, AgentSpec
from agent_backbone.models import EventType
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.github._poller import (
    GitHubPoller,
    PollCheckpoint,
    comment_event_from_api,
    issue_event_from_api,
    polled_repos,
)
from tests.conftest import TEST_REPO, make_config

_POLLER = "agent_backbone.services.github._poller"


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
    def test_includes_coordination_and_owned_repos(self, tmp_path):
        config = make_config(
            tmp_path,
            agents=AgentsConfig(
                specs={
                    "a": AgentSpec(name="a", dir="/x", repo="acme/app"),
                    "b": AgentSpec(name="b", dir="/y", repo="acme/app"),
                    "c": AgentSpec(name="c", dir="/z", repo=TEST_REPO),
                }
            ),
        )
        assert polled_repos(config) == [TEST_REPO, "acme/app"]


class TestCheckpoint:
    def test_defaults_to_recent_lookback_then_persists(self, tmp_path):
        path = tmp_path / "cp.json"
        cp = PollCheckpoint(path)
        first = cp.since("a/b")
        assert first.endswith("Z")
        cp.advance("a/b", "2026-08-31T10:00:01Z")
        cp.save()
        assert json.loads(path.read_text()) == {"since": {"a/b": "2026-08-31T10:00:01Z"}}
        assert PollCheckpoint(path).since("a/b") == "2026-08-31T10:00:01Z"

    def test_corrupt_file_is_ignored(self, tmp_path):
        path = tmp_path / "cp.json"
        path.write_text("{{")
        assert PollCheckpoint(path).since("a/b")


@pytest.fixture
async def db():
    async with BackboneDB.connect() as db:
        yield db


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


class TestGitHubPoller:
    async def test_dispatches_new_issue_and_comment_once(self, config, db, services, tmp_path):
        delivery, dispatch = services
        gh = AsyncMock()
        gh.list_issues_since = AsyncMock(return_value=[_issue(1, labels=["for:ike"])])
        gh.list_comments_since = AsyncMock(return_value=[_comment(9, 1, "[from:leo] hi")])
        seed = PollCheckpoint(tmp_path / "cp")
        seed.advance(TEST_REPO, "2026-08-31T09:00:00Z")
        seed.save()
        poller = GitHubPoller(config, db, gh, delivery, dispatch, checkpoint_path=tmp_path / "cp")

        first = await poller.run()
        second = await poller.run()

        assert first == {"dispatch": 2}
        assert dispatch.issue_dispatcher.await_count == 2
        kinds = [c.args[0].event_type for c in dispatch.issue_dispatcher.await_args_list]
        assert kinds == [EventType.ISSUE_OPENED, EventType.COMMENT_CREATED]
        # Second run: same items returned by GitHub are deduplicated
        assert second == {"deduped": 2}
        assert dispatch.issue_dispatcher.await_count == 2
        # Checkpoint advanced past the newest item
        assert gh.list_issues_since.await_args_list[1].args[1] == "2026-08-31T10:05:01Z"

    async def test_closed_issue_goes_to_lifecycle(self, config, db, services, tmp_path):
        delivery, dispatch = services
        gh = AsyncMock()
        gh.list_issues_since = AsyncMock(return_value=[_issue(4, state="closed")])
        gh.list_comments_since = AsyncMock(return_value=[])
        poller = GitHubPoller(config, db, gh, delivery, dispatch, checkpoint_path=tmp_path / "cp")

        summary = await poller.run()

        assert summary == {"lifecycle": 1}
        dispatch.on_issue_closed.assert_awaited_once()

    async def test_comment_on_unlisted_issue_fetches_it(self, config, db, services, tmp_path):
        delivery, dispatch = services
        gh = AsyncMock()
        gh.list_issues_since = AsyncMock(return_value=[])
        gh.list_comments_since = AsyncMock(return_value=[_comment(9, 7, "hello")])
        gh.get_issue_raw = AsyncMock(return_value=_issue(7, labels=["for:ike"]))
        poller = GitHubPoller(config, db, gh, delivery, dispatch, checkpoint_path=tmp_path / "cp")

        await poller.run()

        gh.get_issue_raw.assert_awaited_once_with(7, TEST_REPO)
        event = dispatch.issue_dispatcher.await_args.args[0]
        assert event.issue.number == 7 and event.comment.body == "hello"

    async def test_api_failure_is_nonfatal(self, config, db, services, tmp_path):
        delivery, dispatch = services
        gh = AsyncMock()
        gh.list_issues_since = AsyncMock(side_effect=RuntimeError("rate limited"))
        poller = GitHubPoller(config, db, gh, delivery, dispatch, checkpoint_path=tmp_path / "cp")
        assert await poller.run() == {}

    async def test_dedup_survives_restart_via_db(self, config, db, services, tmp_path):
        delivery, dispatch = services
        gh = AsyncMock()
        gh.list_issues_since = AsyncMock(return_value=[_issue(1, labels=["for:ike"])])
        gh.list_comments_since = AsyncMock(return_value=[])
        with patch(f"{_POLLER}.PollCheckpoint.since", return_value="2026-08-31T09:00:00Z"):
            await GitHubPoller(config, db, gh, delivery, dispatch, tmp_path / "a").run()
            db._seen_deliveries.clear()  # simulate a restart: hot cache gone, DB remains
            summary = await GitHubPoller(config, db, gh, delivery, dispatch, tmp_path / "b").run()
        assert summary == {"deduped": 1}
