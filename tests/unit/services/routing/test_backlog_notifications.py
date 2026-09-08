"""Close and review lifecycle replay regressions using the real database."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.models import (
    EventType,
    IssueData,
    IssueEvent,
    ParsedLabels,
    ReviewData,
    review_source_key,
)
from agent_backbone.services.github import review_started_event
from agent_backbone.services.jobs.github_poll import issue_event_from_api
from agent_backbone.services.routing import dispatch_event
from agent_backbone.services.routing._ingest import _dedup_id


def _review(kind, *, sha="abc123", reviewer="reviewer[bot]", delivery="hook"):
    return IssueEvent(
        event_type=kind,
        delivery_id=delivery,
        issue=IssueData(number=1, repo_full_name="acme/app", is_pull_request=True),
        review=ReviewData(id=7, commit_id=sha, state="commented", user_login=reviewer),
    )


async def test_finished_review_suppresses_late_start_across_replay(config, db):
    finished = _review(EventType.REVIEW_SUBMITTED)
    started = _review(EventType.REVIEW_STARTED, reviewer="reviewer", delivery="late")
    with patch(
        "agent_backbone.services.routing._ingest.issue_dispatcher", new_callable=AsyncMock
    ) as route:
        route.return_value.delivered = []
        route.return_value.offline = []
        route.return_value.deferred = []
        await dispatch_event(finished, config, db, None)
        assert "already finished" in await dispatch_event(started, config, db, None)
        assert route.await_count == 1
        # Another commit remains a new review even for the same PR and reviewer.
        await dispatch_event(
            _review(EventType.REVIEW_STARTED, sha="new", delivery="new"), config, db, None
        )
        assert route.await_count == 2


async def test_finishing_retires_queued_start_and_failed_outbox(db):
    started = _review(EventType.REVIEW_STARTED, reviewer="reviewer")
    key = review_source_key(started.issue, started.review)
    event_id = await db.events.record(delivery_id=key, source="poll", event_type="review_started")
    await db.queue.enqueue(
        session_name="a", message="started", source_key=key, delivery_kind="review"
    )
    await db.outbox.plan(event_id, [{"session_name": "a", "source_key": key}])
    await db.events.finish_review(key)
    assert await db.events.review_finished(key)
    assert (await db.outbox.entries(event_id))[0]["status"] == "skipped"
    assert await db.queue.pending_count("a") == 0


async def test_only_configured_reviewer_pr_comments_are_suppressed(config, db):
    from agent_backbone.models import CommentData

    config = replace(config, github=replace(config.github, reviewers=("reviewer",)))
    event = IssueEvent(
        event_type=EventType.COMMENT_CREATED,
        delivery_id="a",
        issue=IssueData(number=1, repo_full_name="acme/app", is_pull_request=True),
        comment=CommentData(id=1, user_login="reviewer[bot]", body="processing"),
    )
    assert "lifecycle-only" in await dispatch_event(event, config, db, None)
    with patch(
        "agent_backbone.services.routing._ingest.issue_dispatcher", new_callable=AsyncMock
    ) as route:
        route.return_value.delivered = route.return_value.offline = route.return_value.deferred = []
        event.issue.is_pull_request = False
        event.comment.id = 2
        await dispatch_event(event, config, db, None)
        route.assert_awaited_once()


def test_close_identity_survives_ack_edits_and_both_transports():
    item = {
        "number": 1,
        "state": "closed",
        "closed_at": "2026-09-05T14:55:00Z",
        "updated_at": "2026-09-05T15:19:00Z",
    }
    assert issue_event_from_api(item, "acme/app", "2026-09-05T15:00:00Z") is None
    first = issue_event_from_api(item, "acme/app", "2026-09-05T14:00:00Z")
    item["updated_at"] = "2026-09-05T15:55:00Z"
    again = issue_event_from_api(item, "acme/app", "2026-09-05T14:00:00Z")
    webhook = IssueEvent.from_webhook(
        "issues", "closed", {"issue": item, "repository": {"full_name": "ACME/app"}}, "webhook"
    )
    assert _dedup_id(first) == _dedup_id(again) == _dedup_id(webhook)
    item["closed_at"] = "2026-09-05T16:00:00Z"
    reopened_closed = issue_event_from_api(item, "acme/app", "2026-09-05T14:00:00Z")
    assert _dedup_id(reopened_closed) != _dedup_id(first)


async def test_close_replay_notifies_opener_once(config, db):
    issue = IssueData(
        number=77,
        repo_full_name="acme/app",
        state="closed",
        closed_at="2026-09-05T14:55:00Z",
        labels=ParsedLabels(sender="leo"),
    )
    event = IssueEvent(event_type=EventType.ISSUE_CLOSED, issue=issue, delivery_id="first")
    with patch(
        "agent_backbone.services.routing._outbox.safe_deliver", new_callable=AsyncMock
    ) as deliver:
        from agent_backbone.models import DeliveryOutcome
        from agent_backbone.services.routing._delivery import DeliveryReport

        async def receipt(**kwargs):
            await kwargs["on_report"](DeliveryReport(outcome=DeliveryOutcome.DELIVERED))
            return DeliveryOutcome.DELIVERED

        deliver.side_effect = receipt
        await dispatch_event(event, config, db, AsyncMock())
        event.delivery_id = "replayed-after-comment"
        assert "deduped" in await dispatch_event(event, config, db, AsyncMock())
        deliver.assert_awaited_once()


def test_completed_check_is_not_a_review_start_or_verdict():
    check = {
        "id": 1,
        "status": "completed",
        "head_sha": "abc",
        "started_at": "now",
        "conclusion": "success",
    }
    assert review_started_event(check, {"number": 1}, "acme/app", "a") is None
    check["status"] = "in_progress"
    event = review_started_event(check, {"number": 1, "head": {"sha": "abc"}}, "acme/app", "a")
    assert event.event_type == EventType.REVIEW_STARTED and event.review.commit_id == "abc"


async def test_close_replay_keeps_queued_receipt_when_event_mark_failed(config, db):
    from agent_backbone.models import DeliveryOutcome
    from agent_backbone.services.routing._delivery import DeliveryReport

    issue = IssueData(
        number=78,
        repo_full_name="acme/app",
        state="closed",
        closed_at="2026-09-05T14:55:00Z",
        labels=ParsedLabels(sender="leo"),
    )
    event = IssueEvent(event_type=EventType.ISSUE_CLOSED, issue=issue, delivery_id="first")
    finish = db.outbox.finish_event

    async def queue_delivery(**kw):
        await db.queue.enqueue(
            session_name=kw["session_name"],
            message=kw["message"],
            repo=kw["repo"],
            issue_number=kw["issue_number"],
            source_key=kw["source_key"],
            delivery_kind="watch",
        )
        await kw["on_report"](DeliveryReport(DeliveryOutcome.AGENT_WORKING, queue="stored"))
        return DeliveryOutcome.AGENT_WORKING

    with (
        patch(
            "agent_backbone.services.routing._outbox.safe_deliver", side_effect=queue_delivery
        ) as deliver,
        patch.object(db.outbox, "finish_event", side_effect=OSError("temporary")),
    ):
        await dispatch_event(event, config, db, AsyncMock())
    assert await db.queue.pending_count("leo") == 1
    event.delivery_id = "retry"
    await dispatch_event(event, config, db, AsyncMock())
    assert await db.queue.pending_count("leo") == 1
    deliver.assert_awaited_once()
    assert finish is not None


async def test_start_plan_after_completion_is_skipped_atomically(db):
    started = _review(EventType.REVIEW_STARTED)
    key = review_source_key(started.issue, started.review)
    event_id = await db.events.record(delivery_id=key, source="poll", event_type="review_started")
    await db.events.finish_review(key)
    await db.outbox.plan(event_id, [{"session_name": "a", "source_key": key}])
    assert (await db.outbox.entries(event_id))[0]["status"] == "skipped"


async def test_start_plan_before_completion_is_retired(db):
    started = _review(EventType.REVIEW_STARTED)
    key = review_source_key(started.issue, started.review)
    event_id = await db.events.record(delivery_id=key, source="poll", event_type="review_started")
    await db.outbox.plan(event_id, [{"session_name": "a", "source_key": key}])
    assert not await db.events.review_finished(key)
    await db.events.finish_review(key)
    assert (await db.outbox.entries(event_id))[0]["status"] == "skipped"


async def test_new_close_retires_old_failed_close_outbox(db):
    first = await db.events.record(
        delivery_id="close1",
        source="poll",
        event_type="issue_closed",
        repo="acme/app",
        issue_number=1,
    )
    second = await db.events.record(
        delivery_id="close2",
        source="poll",
        event_type="issue_closed",
        repo="acme/app",
        issue_number=1,
    )
    for event_id in (first, second):
        await db.outbox.plan(event_id, [{"session_name": "a"}])
    await db.outbox.discard_issue("acme/app", 1, keep_event_id=second)
    assert (await db.outbox.entries(first))[0]["status"] == "skipped"
    assert (await db.outbox.entries(second))[0]["status"] == "pending"


@pytest.mark.parametrize("arrival", [("old", "new"), ("new", "old")])
async def test_old_close_replay_preserves_newer_receipt_and_skips_lifecycle(config, db, arrival):
    ids = {}
    times = {"old": "2026-09-05T14:00:00Z", "new": "2026-09-05T15:00:00Z"}
    for name in arrival:
        ids[name] = await db.events.record(
            delivery_id=f"closed:acme/app#1@{times[name]}",
            source="poll",
            event_type="issue_closed",
            repo="acme/app",
            issue_number=1,
        )
        await db.outbox.plan(ids[name], [{"session_name": "leo"}])
    old = IssueEvent(
        event_type=EventType.ISSUE_CLOSED,
        delivery_id="replay",
        issue=IssueData(
            number=1, repo_full_name="acme/app", state="closed", closed_at=times["old"]
        ),
    )
    with patch("agent_backbone.services.routing._ingest.on_issue_closed") as lifecycle:
        assert "superseded" in await dispatch_event(old, config, db, AsyncMock())
    lifecycle.assert_not_called()
    assert (await db.outbox.entries(ids["old"]))[0]["status"] == "skipped"
    assert (await db.outbox.entries(ids["new"]))[0]["status"] == "pending"


async def test_completion_between_outbox_check_and_delivery_suppresses_start(config, db):
    started = _review(EventType.REVIEW_STARTED)
    started.issue.labels = ParsedLabels(targets=["leo"])
    key = review_source_key(started.issue, started.review)
    checked, release = asyncio.Event(), asyncio.Event()
    original = db.events.review_finished
    first = True

    async def paused_check(source_key):
        nonlocal first
        result = await original(source_key)
        # First check is in ingestion; pause the second, in the outbox.
        if first:
            first = False
        elif not checked.is_set():
            checked.set()
            await release.wait()
        return result

    with (
        patch.object(db.events, "review_finished", side_effect=paused_check),
        patch("agent_backbone.services.routing._delivery.send_message") as send,
    ):
        async with asyncio.timeout(5):
            task = asyncio.create_task(dispatch_event(started, config, db, None))
            await checked.wait()
            await db.events.finish_review(key)
            release.set()
            await task
    send.assert_not_called()


async def test_completion_waits_for_active_review_start_delivery(config, db):
    from agent_backbone.services.routing import safe_deliver
    from agent_backbone.services.routing.models import SessionIntelligence, SessionProfile

    started = _review(EventType.REVIEW_STARTED)
    key = review_source_key(started.issue, started.review)
    entered, release, finishing = asyncio.Event(), asyncio.Event(), asyncio.Event()
    order = []

    async def send(*args, **kwargs):
        entered.set()
        await release.wait()
        order.append("start")
        return True

    async def finish():
        finishing.set()
        await db.events.finish_review(key)
        order.append("finish")

    with (
        patch(
            "agent_backbone.services.routing._delivery.get_session_intelligence",
            return_value=SessionProfile("leo", SessionIntelligence.READY),
        ),
        patch("agent_backbone.services.routing._delivery.send_message", side_effect=send),
    ):
        async with asyncio.timeout(5):
            start_task = asyncio.create_task(
                safe_deliver(
                    "leo", "started", config, db=db, source_key=key, delivery_kind="review"
                )
            )
            await entered.wait()
            finish_task = asyncio.create_task(finish())
            await finishing.wait()
            assert not finish_task.done()
            release.set()
            await asyncio.gather(start_task, finish_task)
    assert order == ["start", "finish"]


async def test_completion_retires_start_already_loaded_by_queue_drain(config, db):
    from agent_backbone.services.jobs.retry import drain_message_queue

    started = _review(EventType.REVIEW_STARTED)
    key = review_source_key(started.issue, started.review)
    await db.queue.enqueue(
        session_name="leo", message="started", source_key=key, delivery_kind="review"
    )
    loaded, release = asyncio.Event(), asyncio.Event()
    dequeue = db.queue.dequeue

    async def paused_dequeue(*args, **kwargs):
        rows = await dequeue(*args, **kwargs)
        loaded.set()
        await release.wait()
        return rows

    with (
        patch.object(db.queue, "dequeue", side_effect=paused_dequeue),
        patch("agent_backbone.services.routing._delivery.send_message") as send,
    ):
        async with asyncio.timeout(5):
            task = asyncio.create_task(
                drain_message_queue(config, db, None, active_sessions=["leo"])
            )
            await loaded.wait()
            await db.events.finish_review(key)
            release.set()
            result = await task
    send.assert_not_called()
    assert result["queue_cleared"] == 1
    assert await db.queue.pending_count("leo") == 0
