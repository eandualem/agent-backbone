"""Queued GitHub resources use the same current-resource policy as outbox replay."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from agent_backbone.models import DeliveryOutcome, IssueData
from agent_backbone.services.jobs.retry import drain_message_queue
from agent_backbone.services.routing import DeliveryReport


@pytest.mark.parametrize("kind", ["comment", "review", "pull_request", "watch"])
@pytest.mark.parametrize("state", ["closed", "deleted", "transient", "open"])
async def test_queued_resource_is_rechecked_before_delivery(config, db, kind, state):
    await db.queue.enqueue(
        session_name="ike",
        message="old notice",
        delivery_kind=kind,
        repo="example/app",
        issue_number=42,
        source_key="comment:example/app#42:123",
    )
    gh = AsyncMock()
    if state in {"deleted", "transient"}:
        response = httpx.Response(
            404 if state == "deleted" else 503,
            request=httpx.Request("GET", "https://api.github.com/repos/example/app/issues/42"),
        )
        gh.get_issue.side_effect = httpx.HTTPStatusError(
            "unavailable", request=response.request, response=response
        )
    else:
        gh.get_issue.return_value = IssueData(
            number=42, title="Work", state=state, repo_full_name="example/app"
        )
    with patch(
        "agent_backbone.services.jobs.retry.safe_deliver",
        AsyncMock(return_value=DeliveryReport(DeliveryOutcome.DELIVERED)),
    ) as send:
        summary = await drain_message_queue(config, db, gh, active_sessions={"ike"})
    gh.get_issue.assert_awaited_once_with(42, repo_full_name="example/app")
    assert send.await_count == (1 if state == "open" else 0)
    assert await db.queue.pending_count("ike") == (1 if state == "transient" else 0)
    assert summary.get("queue_cleared", 0) == (1 if state in {"closed", "deleted"} else 0)


@pytest.mark.parametrize("closure", ["same", "reopened", "newer"])
async def test_explicit_closure_is_delivered_only_for_its_original_close(config, db, closure):
    await db.queue.enqueue(
        session_name="ike",
        message="closed notice",
        delivery_kind="comment",
        repo="example/app",
        issue_number=42,
        source_key="closed:example/app#42@2026-09-09T00:00:00Z",
    )
    gh = AsyncMock()
    gh.get_issue.return_value = IssueData(
        number=42,
        title="Work",
        repo_full_name="example/app",
        state="open" if closure == "reopened" else "closed",
        closed_at="2026-09-09T00:00:00Z" if closure != "newer" else "2026-09-09T01:00:00Z",
    )
    with patch(
        "agent_backbone.services.jobs.retry.safe_deliver",
        AsyncMock(return_value=DeliveryReport(DeliveryOutcome.DELIVERED)),
    ) as send:
        await drain_message_queue(config, db, gh, active_sessions={"ike"})
    assert send.await_count == (1 if closure == "same" else 0)
    assert await db.queue.pending_count("ike") == 0


async def test_github_unavailability_does_not_hold_direct_messages(config, db):
    for kind in ("review", "direct_message"):
        await db.queue.enqueue(
            session_name="ike",
            message=kind,
            delivery_kind=kind,
            repo="example/app",
            issue_number=42,
        )
    with patch(
        "agent_backbone.services.jobs.retry.safe_deliver",
        AsyncMock(return_value=DeliveryReport(DeliveryOutcome.DELIVERED)),
    ) as send:
        await drain_message_queue(config, db, None, active_sessions={"ike"})
    send.assert_awaited_once()
    assert send.await_args.kwargs["delivery_kind"] == "direct_message"
    assert await db.queue.pending_count("ike") == 1
