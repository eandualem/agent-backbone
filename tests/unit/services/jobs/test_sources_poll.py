"""The sources-poll job: filters from subscriptions, a cursor per source."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from agent_backbone.config import AgentsConfig, AgentSpec, Subscription
from agent_backbone.services.jobs.sources_poll import SourcesPoller, subscription_filters
from agent_backbone.services.sources import Source, SourceEvent, Sources
from tests.conftest import make_config


class _Recorder(Source):
    name = "gmail"

    def __init__(self, config, events=(), fail=False):
        super().__init__(config)
        self.calls: list[tuple[list[str], datetime]] = []
        self.events = list(events)
        self.fail = fail

    @property
    def enabled(self) -> bool:
        return True

    async def poll(self, filters, since):
        self.calls.append((list(filters), since))
        if self.fail:
            raise RuntimeError("imap down")
        return self.events


def _config(tmp_path):
    specs = {
        "desk": AgentSpec(
            name="desk",
            dir=str(tmp_path / "desk"),
            subscriptions=(
                Subscription(1, "gmail", "from:upwork.com", "high"),
                Subscription(2, "gmail", "from:linkedin.com", "normal"),
            ),
        ),
        "other": AgentSpec(
            name="other",
            dir=str(tmp_path / "other"),
            subscriptions=(Subscription(3, "gmail", "from:upwork.com", "normal"),),
        ),
    }
    return make_config(tmp_path, agents=AgentsConfig(specs=specs))


def test_filters_are_distinct_per_source(tmp_path):
    config = _config(tmp_path)
    assert subscription_filters(config, "gmail") == ["from:linkedin.com", "from:upwork.com"]
    assert subscription_filters(config, "other") == []


async def test_first_run_starts_now_and_the_cursor_advances(tmp_path, db):
    config = _config(tmp_path)
    event = SourceEvent(
        "gmail", "a1", "x", "y", datetime.now(UTC), "link", frozenset({"from:upwork.com"})
    )
    source = _Recorder(config, events=[event])
    poller = SourcesPoller(config, db, Sources([source]))
    started = datetime.now(UTC)
    with patch(
        "agent_backbone.services.jobs.sources_poll.dispatch_source_events",
        new_callable=AsyncMock,
        return_value={"events": 1, "delivered": 2},
    ) as dispatch:
        assert await poller.run() == {"events": 1, "delivered": 2}
    filters, since = source.calls[0]
    assert filters == ["from:linkedin.com", "from:upwork.com"]
    # No mailbox history: the window opens at the poll (minus the overlap).
    assert (started - since).total_seconds() < 150
    dispatch.assert_awaited_once()
    assert dispatch.await_args.args[0] == [event]
    cursor = await db.events.poll_cursor("source:gmail")
    assert cursor is not None and cursor >= started.replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


async def test_a_failed_poll_keeps_the_cursor_and_is_observed(tmp_path, db):
    config = _config(tmp_path)
    await db.events.save_poll_cursor("source:gmail", "2026-09-17T10:00:00Z")
    poller = SourcesPoller(config, db, Sources([_Recorder(config, fail=True)]))
    assert await poller.run() == {}
    assert await db.events.poll_cursor("source:gmail") == "2026-09-17T10:00:00Z"
    failures = [
        row
        for row in await db.diagnostics.query(limit=20)
        if row["source"] == "sources-poll" and row["severity"] != "info"
    ]
    assert failures and failures[0]["details"]["error_type"] == "RuntimeError"


async def test_no_subscriptions_means_no_poll(tmp_path, db):
    config = make_config(tmp_path)
    source = _Recorder(config)
    assert await SourcesPoller(config, db, Sources([source])).run() == {}
    assert source.calls == []
