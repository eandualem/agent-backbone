"""Real database regressions for partial GitHub fan-out and durable retry."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import HTTPStatusError, Request, Response
from sqlalchemy import text

from agent_backbone.config import AgentsConfig, sqlite_url
from agent_backbone.models import CommentData, EventType, IssueData, IssueEvent, ParsedLabels
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.routing import dispatch_event, retry_outbox
from agent_backbone.services.routing._delivery import safe_deliver
from agent_backbone.services.routing.models import SessionIntelligence, SessionProfile
from tests.conftest import TEST_REPO
from tests.support import queue_row

_DELIVERY = "agent_backbone.services.routing._delivery"
_OUTBOX = "agent_backbone.services.routing._outbox"


def event() -> IssueEvent:
    return IssueEvent(
        event_type=EventType.COMMENT_CREATED,
        delivery_id="webhook-outbox-1",
        issue=IssueData(
            number=42,
            repo_full_name=TEST_REPO,
            title="two recipients",
            labels=ParsedLabels(targets=["ike", "leo"]),
        ),
        comment=CommentData(id=321, body="Please check this change", user_login="human"),
    )


def ready(session, config, **kwargs):
    return SessionProfile(session_name=session, intelligence=SessionIntelligence.READY)


async def test_failed_queue_write_retries_only_unresolved_recipient(config, db):
    reads = []

    async def readiness(session, config, **kwargs):
        reads.append(session)
        return SessionProfile(
            session_name=session,
            intelligence=(
                SessionIntelligence.READY if session == "ike" else SessionIntelligence.AGENT_WORKING
            ),
        )

    async def sent(session, message, **kwargs):
        # The complete audience already exists before the first external paste.
        (stored,) = await db.events.query()
        assert [row["recipient"] for row in await db.outbox.entries(stored["id"])] == ["ike", "leo"]
        return True

    with (
        patch(f"{_DELIVERY}.get_session_intelligence", side_effect=readiness),
        patch(f"{_DELIVERY}.send_message", side_effect=sent) as send,
    ):
        with patch.object(db.queue, "enqueue", side_effect=RuntimeError("database busy")):
            outcome = await dispatch_event(event(), config, db, None)
        assert outcome.startswith("deferred: outbox")
        (stored,) = await db.events.query()
        assert stored["processed_at"] is None
        assert [row["status"] for row in await db.outbox.entries(stored["id"])] == [
            "delivered",
            "failed",
        ]

        summary = await retry_outbox(config, db, None)
        assert summary["outbox_completed"] == 1
        assert reads == ["ike", "leo", "leo"]
        assert [call.args[0] for call in send.await_args_list] == ["ike"]
        assert (await queue_row(db, 1))["session_name"] == "leo"
        assert (await db.events.query())[0]["processed_at"] is not None
        assert (await dispatch_event(event(), config, db, None)).startswith("deduped:")


async def test_restart_after_first_recipient_does_not_resend_it(config, tmp_path):
    calls = 0

    async def crash_before_second(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise asyncio.CancelledError
        return await safe_deliver(**kwargs)

    url = sqlite_url(tmp_path / "outbox.db")
    with (
        patch(f"{_DELIVERY}.get_session_intelligence", side_effect=ready),
        patch(f"{_DELIVERY}.send_message", AsyncMock(return_value=True)) as send,
    ):
        async with BackboneDB.connect(url) as db:
            with (
                patch(f"{_OUTBOX}.safe_deliver", side_effect=crash_before_second),
                pytest.raises(asyncio.CancelledError),
            ):
                await dispatch_event(event(), config, db, None)
            assert (await db.events.query())[0]["processed_at"] is None

        async with BackboneDB.connect(url) as db:
            await dispatch_event(event(), config, db, None)
            assert (await db.events.query())[0]["processed_at"] is not None
        assert [call.args[0] for call in send.await_args_list] == ["ike", "leo"]


@pytest.mark.parametrize("repository,method", [("events", "record"), ("outbox", "plan")])
async def test_storage_failure_never_sends(config, db, repository, method):
    with (
        patch.object(getattr(db, repository), method, side_effect=RuntimeError("disk full")),
        patch(f"{_DELIVERY}.send_message", AsyncMock()) as send,
        pytest.raises(RuntimeError, match="disk full"),
    ):
        await dispatch_event(event(), config, db, None)
    send.assert_not_awaited()


async def test_retry_during_dispatch_does_not_duplicate_a_recipient(config, db):
    started = asyncio.Event()
    release = asyncio.Event()

    async def send(session, message, **kwargs):
        started.set()
        await release.wait()
        return True

    with (
        patch(f"{_DELIVERY}.get_session_intelligence", side_effect=ready),
        patch(f"{_DELIVERY}.send_message", side_effect=send) as sent,
    ):
        initial = asyncio.create_task(dispatch_event(event(), config, db, None))
        await asyncio.wait_for(started.wait(), 2)
        retry = asyncio.create_task(retry_outbox(config, db, None))
        release.set()
        await asyncio.wait_for(asyncio.gather(initial, retry), 2)
        assert [call.args[0] for call in sent.await_args_list] == ["ike", "leo"]


async def test_closing_issue_retires_pending_recipients(config, db):
    with patch(f"{_DELIVERY}.get_session_intelligence", side_effect=RuntimeError("unavailable")):
        await dispatch_event(event(), config, db, None)
    closed = IssueEvent(
        delivery_id="close-outbox-issue",
        event_type=EventType.ISSUE_CLOSED,
        issue=event().issue.model_copy(update={"state": "closed"}),
    )
    await dispatch_event(closed, config, db, None)
    with patch(f"{_DELIVERY}.send_message", AsyncMock()) as send:
        await retry_outbox(config, db, None)
    send.assert_not_awaited()
    assert await db.outbox.pending_events() == []


async def test_event_retention_preserves_unresolved_recipients(config, db):
    with patch(f"{_DELIVERY}.get_session_intelligence", side_effect=RuntimeError("unavailable")):
        await dispatch_event(event(), config, db, None)
    (stored,) = await db.events.query()
    async with db.engine.begin() as conn:
        await conn.execute(text("UPDATE events SET received_at = '2000-01-01T00:00:00Z'"))
    assert await db.events.prune(retention_days=1) == 0
    assert len(await db.outbox.entries(stored["id"])) == 2
    await db.outbox.discard_issue(TEST_REPO, 42)
    assert await db.events.prune(retention_days=1) == 1
    assert await db.outbox.entries(stored["id"]) == []


async def test_existing_issue_claim_is_not_a_completed_receipt(config, db):
    config = replace(config, agents=AgentsConfig(specs={"ike": config.agents.get("ike")}))
    issue = event().issue.model_copy(update={"labels": ParsedLabels(targets=["ike"])})
    opened = IssueEvent(event_type=EventType.ISSUE_OPENED, issue=issue, delivery_id="opened-42")
    claim = await db.deliveries.claim(
        issue_number=42, target_entity="ike", session_name="ike", source="monitor", repo=TEST_REPO
    )
    with patch(f"{_DELIVERY}.send_message", AsyncMock()) as send:
        await dispatch_event(opened, config, db, None)
        assert (await db.events.query())[0]["processed_at"] is None
        await db.deliveries.finalize(claim, "delivered")
        await retry_outbox(config, db, None)
    send.assert_not_awaited()
    assert (await db.events.query())[0]["processed_at"] is not None


async def test_failed_issue_plan_is_not_suppressed_by_recent_notification(config, db):
    config = replace(config, agents=AgentsConfig(specs={"ike": config.agents.get("ike")}))
    issue = event().issue.model_copy(update={"labels": ParsedLabels(targets=["ike"])})
    opened = IssueEvent(event_type=EventType.ISSUE_OPENED, issue=issue, delivery_id="opened-42")
    with (
        patch.object(db.outbox, "plan", side_effect=RuntimeError("disk full")),
        pytest.raises(RuntimeError),
    ):
        await dispatch_event(opened, config, db, None)
    with (
        patch(f"{_DELIVERY}.get_session_intelligence", side_effect=ready),
        patch(f"{_DELIVERY}.send_message", AsyncMock(return_value=True)) as send,
    ):
        await dispatch_event(opened, config, db, None)
    assert [call.args[0] for call in send.await_args_list] == ["ike"]
    assert (await db.events.query())[0]["processed_at"] is not None


async def test_completed_receipts_recover_a_failed_event_mark(config, db):
    with (
        patch(f"{_DELIVERY}.get_session_intelligence", side_effect=ready),
        patch(f"{_DELIVERY}.send_message", AsyncMock(return_value=True)) as send,
    ):
        with patch.object(db.outbox, "finish_event", side_effect=RuntimeError("database busy")):
            await dispatch_event(event(), config, db, None)
        assert (await db.events.query())[0]["processed_at"] is None
        await retry_outbox(config, db, None)
    assert [call.args[0] for call in send.await_args_list] == ["ike", "leo"]
    assert (await db.events.query())[0]["processed_at"] is not None


@pytest.mark.parametrize("status", [404, 503])
async def test_retry_retires_missing_issue_but_preserves_service_failures(config, db, status):
    with patch(f"{_DELIVERY}.get_session_intelligence", side_effect=RuntimeError("unavailable")):
        await dispatch_event(event(), config, db, None)
    response = Response(status, request=Request("GET", "https://api.github.com/repos/acme/app"))
    gh = AsyncMock()
    gh.get_issue.side_effect = HTTPStatusError(
        "unavailable", request=response.request, response=response
    )
    with patch(f"{_DELIVERY}.send_message", AsyncMock()) as send:
        await retry_outbox(config, db, gh)
    send.assert_not_awaited()
    assert ((await db.events.query())[0]["processed_at"] is not None) == (status == 404)
