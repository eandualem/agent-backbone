"""Tests for the GitHub polling connector (poll intake + startup backfill)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_backbone.config import AgentsConfig, AgentSpec
from agent_backbone.models import EventType
from agent_backbone.services.jobs.github_poll import (
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
def dispatch():
    """The routing entry points behind ``dispatch_event``."""
    mocks = SimpleNamespace(
        issue_dispatcher=AsyncMock(
            return_value=MagicMock(delivered=["ike"], offline=[], deferred=[])
        ),
        on_issue_closed=AsyncMock(return_value={"ike": "queue_empty"}),
    )
    with (
        patch("agent_backbone.services.routing._ingest.issue_dispatcher", mocks.issue_dispatcher),
        patch("agent_backbone.services.routing._ingest.on_issue_closed", mocks.on_issue_closed),
    ):
        yield mocks


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
    async def test_dispatches_new_issue_and_comment_once(self, config, db, dispatch, frozen_now):
        gh = AsyncMock()
        gh.list_issues_since = AsyncMock(return_value=[_issue(1, labels=["for:ike"])])
        gh.list_comments_since = AsyncMock(return_value=[_comment(9, 1, "[from:leo] hi")])
        poller = GitHubPoller(lambda: config, db, gh)
        poller._since[TEST_REPO] = "2026-08-31T09:00:00Z"

        first = await poller.run()
        second = await poller.run()

        assert first == {"dispatch": 2}
        assert dispatch.issue_dispatcher.await_count == 2
        kinds = [c.args[0].event_type for c in dispatch.issue_dispatcher.await_args_list]
        assert kinds == [EventType.ISSUE_OPENED, EventType.COMMENT_CREATED]
        assert second == {"deduped": 2}
        assert dispatch.issue_dispatcher.await_count == 2
        assert gh.list_issues_since.await_args_list[1].args[1] == "2026-08-31T11:58:00Z"
        # Both events were stored in the activity feed
        assert len(await db.events.query(repo=TEST_REPO)) == 2

    async def test_closed_issue_goes_to_lifecycle(self, config, db, dispatch):
        gh = AsyncMock()
        gh.list_issues_since = AsyncMock(return_value=[_issue(4, state="closed")])
        gh.list_comments_since = AsyncMock(return_value=[])
        poller = GitHubPoller(config, db, gh)

        summary = await poller.run()

        assert summary == {"lifecycle": 1}
        dispatch.on_issue_closed.assert_awaited_once()

    async def test_comment_on_unlisted_issue_fetches_it(self, config, db, dispatch):
        gh = AsyncMock()
        gh.list_issues_since = AsyncMock(return_value=[])
        gh.list_comments_since = AsyncMock(return_value=[_comment(9, 7, "hello")])
        gh.get_issue_raw = AsyncMock(return_value=_issue(7, labels=["for:ike"]))
        poller = GitHubPoller(config, db, gh)

        await poller.run()

        gh.get_issue_raw.assert_awaited_once_with(7, TEST_REPO)
        event = dispatch.issue_dispatcher.await_args.args[0]
        assert event.issue.number == 7 and event.comment.body == "hello"

    async def test_api_failure_is_nonfatal(self, config, db, dispatch):
        gh = AsyncMock()
        gh.list_issues_since = AsyncMock(side_effect=RuntimeError("rate limited"))
        poller = GitHubPoller(config, db, gh)
        assert await poller.run() == {}

    async def test_dedup_survives_restart_via_events_table(self, config, db, dispatch):
        gh = AsyncMock()
        gh.list_issues_since = AsyncMock(return_value=[_issue(1, labels=["for:ike"])])
        gh.list_comments_since = AsyncMock(return_value=[])
        first = GitHubPoller(config, db, gh)
        first._since[TEST_REPO] = "2026-08-31T09:00:00Z"
        await first.run()
        # restart: nothing in memory survives, the events table does

        second = GitHubPoller(config, db, gh)
        second._since[TEST_REPO] = "2026-08-31T09:00:00Z"
        summary = await second.run()

        assert summary == {"deduped": 1}
        assert dispatch.issue_dispatcher.await_count == 1

    async def test_initial_boundary_is_independent_of_receipt_time(self, config, db, dispatch):
        await db.events.record(
            delivery_id="x", source="webhook", event_type="issue_opened", repo=TEST_REPO
        )
        gh = AsyncMock()
        gh.list_issues_since = AsyncMock(return_value=[])
        gh.list_comments_since = AsyncMock(return_value=[])
        await GitHubPoller(config, db, gh).run()
        since = gh.list_issues_since.await_args.args[1]
        # Initial boundaries use lookback; event receipt times do not decide replay
        assert since.endswith("Z") and since >= "2020"


class TestHydrationFailure:
    async def test_comment_whose_issue_cannot_be_fetched_keeps_the_cursor(
        self, config, db, dispatch
    ):
        # S1-1: C1 fails to hydrate, C2 succeeds; the cursor must not move past C1.
        gh = AsyncMock()
        gh.list_issues_since = AsyncMock(return_value=[])
        gh.list_comments_since = AsyncMock(
            return_value=[
                _comment(9, 1, "first", updated="2026-08-31T10:00:00Z"),
                _comment(10, 2, "second", updated="2026-08-31T10:01:00Z"),
            ]
        )

        async def raw(number, repo):
            if number == 1:
                raise RuntimeError("boom")
            return _issue(2)

        gh.get_issue_raw = AsyncMock(side_effect=raw)
        poller = GitHubPoller(lambda: config, db, gh)
        poller._since[TEST_REPO] = "2026-08-31T09:00:00Z"

        await poller.run()

        assert poller._since[TEST_REPO] == "2026-08-31T09:00:00Z"


_POLL = "agent_backbone.services.jobs.github_poll"


@pytest.fixture
def frozen_now():
    with patch(f"{_POLL}.datetime", wraps=datetime) as clock:
        clock.now.return_value = datetime(2026, 8, 31, 12, tzinfo=UTC)
        yield clock


class TestDurableBoundary:
    @pytest.mark.parametrize("empty", [False, True])
    async def test_successful_stationary_or_empty_batch_leaves_old_events_behind(
        self, config, db, dispatch, frozen_now, empty
    ):
        gh = AsyncMock()
        item = _issue(1, updated="2026-08-31T11:59:00Z", labels=["for:ike"])

        async def fetch(repo, since):
            return [item] if not empty and item["updated_at"] >= since else []

        gh.list_issues_since.side_effect = fetch
        gh.list_comments_since.return_value = []
        await GitHubPoller(config, db, gh).run()
        assert await db.events.poll_cursor(TEST_REPO) == "2026-08-31T11:58:00Z"
        frozen_now.now.return_value = datetime(2026, 8, 31, 12, 5, tzinfo=UTC)
        await GitHubPoller(config, db, gh).run()
        assert await db.events.poll_cursor(TEST_REPO) == "2026-08-31T12:03:00Z"
        # Even once the dedup record expires, the old item is outside the
        # successfully completed window and cannot be delivered again.
        await db.events.prune(0)
        await GitHubPoller(config, db, gh).run()
        assert dispatch.issue_dispatcher.await_count == (0 if empty else 1)

    async def test_progress_uses_time_before_fetch_not_time_after_dispatch(
        self, config, db, frozen_now
    ):
        gh = AsyncMock()

        async def slow_fetch(repo, since):
            frozen_now.now.return_value = datetime(2026, 8, 31, 20, tzinfo=UTC)
            return []

        gh.list_issues_since.side_effect = slow_fetch
        gh.list_comments_since.return_value = []
        await GitHubPoller(config, db, gh).run()
        assert await db.events.poll_cursor(TEST_REPO) == "2026-08-31T11:58:00Z"

    @pytest.mark.parametrize("failure", ["hydration", "dispatch"])
    async def test_partial_first_batch_restarts_from_persisted_initial_window(
        self, config, db, frozen_now, failure
    ):
        gh = AsyncMock()
        gh.list_issues_since.return_value = [_issue(2)]
        gh.list_comments_since.return_value = [_comment(9, 1, "first")]
        gh.get_issue_raw.side_effect = (
            RuntimeError("unavailable") if failure == "hydration" else None
        )
        gh.get_issue_raw.return_value = _issue(1)
        fetched_boundaries = []

        async def fetch(repo, since):
            # This assertion is before either API response or event receipt.
            assert await db.events.poll_cursor(repo) == since
            fetched_boundaries.append(since)
            return [_issue(2)]

        gh.list_issues_since.side_effect = fetch

        async def dispatch(event, *args, **kwargs):
            await db.events.record(
                delivery_id=event.delivery_id, source="poll", event_type="test", repo=TEST_REPO
            )
            if failure == "dispatch" and event.comment:
                raise RuntimeError("dispatch failed")
            return "dispatch"

        first = GitHubPoller(config, db, gh)
        with patch(f"{_POLL}.dispatch_event", side_effect=dispatch):
            await first.run()
        boundary = await db.events.poll_cursor(TEST_REPO)
        assert boundary == fetched_boundaries[0]
        assert len(await db.events.query()) >= 1
        # A much later restart must use the stored initial window, even though
        # successful events from the first batch now have newer receipt times.
        frozen_now.now.return_value = datetime(2026, 9, 5, tzinfo=UTC)
        gh.get_issue_raw.side_effect = None
        with patch(f"{_POLL}.dispatch_event", AsyncMock(return_value="dispatch")):
            await GitHubPoller(config, db, gh).run()
        assert fetched_boundaries == [boundary, boundary]

    async def test_successful_restart_keeps_overlap_and_same_second_late_arrival(
        self, config, db, frozen_now
    ):
        gh = AsyncMock()
        gh.list_issues_since.return_value = [_issue(1, updated="2026-08-31T12:00:00Z")]
        gh.list_comments_since.return_value = []
        with patch(f"{_POLL}.dispatch_event", AsyncMock(return_value="dispatch")) as dispatch:
            await GitHubPoller(config, db, gh).run()
            assert await db.events.poll_cursor(TEST_REPO) == "2026-08-31T11:58:00Z"
            gh.list_issues_since.return_value = [
                _issue(1, updated="2026-08-31T12:00:00Z"),
                _issue(2, updated="2026-08-31T12:00:00Z"),
            ]
            await GitHubPoller(config, db, gh).run()
        assert gh.list_issues_since.await_args.args[1] == "2026-08-31T11:58:00Z"
        assert dispatch.await_args.args[0].issue.number == 2

    @pytest.mark.parametrize("phase", ["initial", "advance", "read"])
    async def test_cursor_failure_preserves_boundary_and_isolates_repositories(
        self, config, db, frozen_now, phase
    ):
        config = replace(
            config,
            agents=AgentsConfig(
                specs={
                    "a": AgentSpec(name="a", dir="/a", repo="acme/a"),
                    "b": AgentSpec(name="b", dir="/b", repo="acme/b"),
                }
            ),
        )
        prior = "2026-08-31T09:00:00Z"
        if phase == "advance":
            await db.events.save_poll_cursor("acme/a", prior)
        gh = AsyncMock()
        gh.list_issues_since.return_value = [_issue(1)]
        gh.list_comments_since.return_value = []
        method = "poll_cursor" if phase == "read" else "save_poll_cursor"
        original = getattr(db.events, method)

        async def fail_a(repo, *args):
            if repo == "acme/a":
                raise RuntimeError("database unavailable")
            return await original(repo, *args)

        poller = GitHubPoller(config, db, gh)
        with (
            patch.object(db.events, method, side_effect=fail_a),
            patch(f"{_POLL}.dispatch_event", AsyncMock(return_value="dispatch")),
        ):
            await poller.run()
        assert await db.events.poll_cursor("acme/a") == (prior if phase == "advance" else None)
        assert poller._since.get("acme/a") == (prior if phase == "advance" else None)
        assert await db.events.poll_cursor("acme/b") == "2026-08-31T11:58:00Z"
        if phase != "advance":
            assert [c.args[0] for c in gh.list_issues_since.await_args_list] == ["acme/b"]

    @pytest.mark.parametrize("bad", ["bad", "2026-08-01", "9999-01-01T00:00:00Z"])
    async def test_malformed_or_future_cursor_is_reset_before_fetch(
        self, config, db, frozen_now, bad
    ):
        await db.events.save_poll_cursor(TEST_REPO, bad)
        gh = AsyncMock()

        async def fetch(repo, since):
            assert since != bad
            assert await db.events.poll_cursor(repo) == since
            return []

        gh.list_issues_since.side_effect = fetch
        gh.list_comments_since.return_value = []
        await GitHubPoller(config, db, gh).run()
        gh.list_issues_since.assert_awaited_once()
