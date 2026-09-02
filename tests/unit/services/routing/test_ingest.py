"""Tests for event ingestion: storage, dedup, routing, hooks."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from agent_backbone.models import EventType, IssueData, IssueEvent, ParsedLabels
from agent_backbone.services.routing._ingest import dispatch_event
from tests.conftest import TEST_REPO

_INGEST = "agent_backbone.services.routing._ingest"


def _event(delivery_id: str, event_type: EventType = EventType.ISSUE_OPENED) -> IssueEvent:
    issue = IssueData(
        number=42,
        repo_full_name=TEST_REPO,
        title="x",
        state="closed" if event_type == EventType.ISSUE_CLOSED else "open",
        labels=ParsedLabels(targets=["ike"]),
    )
    return IssueEvent(event_type=event_type, issue=issue, delivery_id=delivery_id)


def _dispatch_result():
    return MagicMock(delivered=["ike"], offline=[], deferred=[])


class TestDispatchEvent:
    async def test_stores_then_routes_then_marks(self, config, db):
        with patch(f"{_INGEST}.issue_dispatcher", AsyncMock(return_value=_dispatch_result())):
            outcome = await dispatch_event(_event("d-1"), config, db, None)
        assert outcome == "dispatch: 1 delivered, 0 offline, 0 deferred"
        (row,) = await db.events.query(repo=TEST_REPO)
        assert row["delivery_id"] == "d-1" and row["outcome"] == outcome
        assert row["processed_at"] is not None

    async def test_a_stored_delivery_is_not_routed_again(self, config, db):
        with patch(f"{_INGEST}.issue_dispatcher", AsyncMock(return_value=_dispatch_result())) as d:
            await dispatch_event(_event("d-1"), config, db, None)
            outcome = await dispatch_event(_event("d-1"), config, db, None)
        assert outcome == "deduped: event d-1 already stored"
        assert d.await_count == 1

    async def test_a_delivery_in_flight_is_not_routed_twice(self, config, db):
        """A retry that lands while the first copy is still routing is dropped."""
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_dispatch(*args, **kwargs):
            started.set()
            await release.wait()
            return _dispatch_result()

        with patch(f"{_INGEST}.issue_dispatcher", AsyncMock(side_effect=slow_dispatch)) as d:
            first = asyncio.create_task(dispatch_event(_event("d-1"), config, db, None))
            await started.wait()
            second = await dispatch_event(_event("d-1"), config, db, None)
            release.set()
            await first
        assert second == "deduped: event d-1 is being routed"
        assert d.await_count == 1

    async def test_issue_closed_runs_the_hooks(self, config, db):
        hook = AsyncMock()
        with patch(f"{_INGEST}.on_issue_closed", AsyncMock(return_value={})):
            outcome = await dispatch_event(
                _event("c-1", EventType.ISSUE_CLOSED),
                config,
                db,
                AsyncMock(),
                issue_closed_hooks=(hook,),
            )
        assert outcome.startswith("lifecycle:")
        hook.assert_awaited_once_with(TEST_REPO, 42)

    async def test_hook_failure_is_isolated(self, config, db):
        with patch(f"{_INGEST}.on_issue_closed", AsyncMock(return_value={})):
            outcome = await dispatch_event(
                _event("c-2", EventType.ISSUE_CLOSED),
                config,
                db,
                AsyncMock(),
                issue_closed_hooks=(AsyncMock(side_effect=RuntimeError("boom")),),
            )
        assert outcome.startswith("lifecycle:")
