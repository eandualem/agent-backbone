"""Delivery evidence follows exact database identities, without message content."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from agent_backbone.services.agents import AgentState
from agent_backbone.services.jobs.retry import drain_message_queue, retry_delivery
from agent_backbone.services.routing import deliver, retry_outbox
from agent_backbone.services.routing._outbox import flush_outbox
from agent_backbone.services.routing.models import SessionIntelligence, SessionProfile
from tests.support import queue_row

_DELIVERY = "agent_backbone.services.routing._delivery"
_SECRET = "private-message-and-provider-token"


def profile(condition=SessionIntelligence.READY, *, reason=None):
    return SessionProfile(
        session_name="ike",
        intelligence=condition,
        agent_state=AgentState.BLOCKED if reason else AgentState.IDLE,
        reason=reason,
        state_source="push",
        runtime="shell",
        last_message=_SECRET,
        detail=_SECRET,
        evidence=[_SECRET],
    )


async def test_repeated_block_then_drain_shares_one_operation_and_groups_deferrals(config, db):
    blocked = profile(SessionIntelligence.AGENT_WORKING, reason="provider")
    with (
        patch(f"{_DELIVERY}.get_session_intelligence", return_value=blocked) as state,
        patch(f"{_DELIVERY}.send_message", return_value=True) as send,
    ):
        original = await deliver(
            "ike", _SECRET, config, db=db, delivery_kind="direct_message", sender="alice"
        )
        duplicate = await deliver(
            "ike", _SECRET, config, db=db, delivery_kind="direct_message", sender="alice"
        )
        for _ in range(3):
            await drain_message_queue(config, db, None, active_sessions={"ike"})
        assert duplicate.operation_id == original.operation_id
        assert duplicate.queue_id == original.queue_id
        assert duplicate.queue == "already_queued"
        send.assert_not_awaited()
        evidence = await db.diagnostics.query(operation_id=original.operation_id)
        deferred = next(row for row in evidence if row["code"] == "deferred_agent_working_provider")
        assert deferred["occurrences"] == 5
        assert deferred["details"]["state"] == "blocked"
        assert deferred["details"]["reason"] == "provider"
        assert deferred["details"]["state_source"] == "push"
        assert not any(row["code"] == "submitted" for row in evidence)
        assert _SECRET not in json.dumps(evidence)

        state.return_value = profile()
        await drain_message_queue(config, db, None, active_sessions={"ike"})
        send.assert_awaited_once()
    row = await queue_row(db, original.queue_id)
    assert row["status"] == "delivered"
    assert row["operation_id"] == original.operation_id
    receipts = await db.deliveries.query(session_name="ike")
    assert {row["operation_id"] for row in receipts} == {original.operation_id}
    submitted = next(
        row
        for row in await db.diagnostics.query(operation_id=original.operation_id)
        if row["code"] == "submitted"
    )
    assert submitted["queue_id"] == original.queue_id
    assert submitted["delivery_id"] == receipts[0]["id"]


async def test_unrelated_success_does_not_recover_a_failed_message(config, db):
    with (
        patch(f"{_DELIVERY}.get_session_intelligence", return_value=profile()),
        patch(f"{_DELIVERY}.send_message", side_effect=[False, True]),
    ):
        failed = await deliver("ike", _SECRET, config, db=db, delivery_kind="direct_message")
        # Even identical text is a new attempt when it is delivered immediately.
        successful = await deliver("ike", _SECRET, config, db=db, delivery_kind="direct_message")
    assert failed.operation_id != successful.operation_id
    assert failed.queue == "stored"
    evidence = await db.diagnostics.query(operation_id=failed.operation_id)
    assert any(
        row["code"] == "submission_unconfirmed" and row["severity"] == "error" for row in evidence
    )
    assert not any(row["code"] == "submitted" for row in evidence)
    assert (await queue_row(db, failed.queue_id))["status"] == "pending"


async def test_different_senders_with_same_text_have_different_operations(config, db):
    with patch(
        f"{_DELIVERY}.get_session_intelligence",
        return_value=profile(SessionIntelligence.AGENT_WORKING),
    ):
        first = await deliver(
            "ike", _SECRET, config, db=db, delivery_kind="direct_message", sender="alice"
        )
        second = await deliver(
            "ike", _SECRET, config, db=db, delivery_kind="direct_message", sender="bob"
        )
    assert first.operation_id != second.operation_id
    assert first.queue_id != second.queue_id


async def test_queue_write_failure_is_distinct_from_safe_deferral_and_redacted(config, db, caplog):
    with (
        patch(
            f"{_DELIVERY}.get_session_intelligence",
            return_value=profile(SessionIntelligence.AGENT_WORKING),
        ),
        patch.object(db.queue, "enqueue", side_effect=RuntimeError(_SECRET)),
        patch(f"{_DELIVERY}.send_message") as send,
    ):
        result = await deliver("ike", _SECRET, config, db=db, delivery_kind="direct_message")
    send.assert_not_awaited()
    assert result.queue == "failed"
    assert result.queue_id is None
    evidence = await db.diagnostics.query(operation_id=result.operation_id)
    failure = next(row for row in evidence if row["code"] == "queue_storage_failed")
    assert failure["severity"] == "error"
    assert failure["details"]["error_type"] == "RuntimeError"
    assert failure["delivery_id"] == result.delivery_id
    assert not any(row["code"] == "submission_unconfirmed" for row in evidence)
    assert _SECRET not in json.dumps(evidence)
    assert _SECRET not in caplog.text


@pytest.mark.parametrize(
    "stage, code",
    [("readiness", "readiness_failed"), ("submission", "submission_unconfirmed")],
)
async def test_delivery_exceptions_leave_evidence_without_claiming_submission(
    config, db, stage, code
):
    with (
        patch(f"{_DELIVERY}.get_session_intelligence", return_value=profile()) as state,
        patch(f"{_DELIVERY}.send_message", return_value=True) as send,
    ):
        failing = state if stage == "readiness" else send
        failing.side_effect = RuntimeError(_SECRET)
        with pytest.raises(RuntimeError, match=_SECRET):
            await deliver("ike", _SECRET, config, db=db, delivery_kind="direct_message")
    evidence = await db.diagnostics.query()
    assert [row["code"] for row in evidence] == [code]
    assert evidence[0]["details"]["stage"] == stage
    assert evidence[0]["details"]["error_type"] == "RuntimeError"
    assert _SECRET not in json.dumps(evidence)
    assert await db.queue.pending_count("ike") == 0


@pytest.mark.parametrize("legacy", [False, True])
async def test_expiry_is_correlated_and_never_a_submission(config, db, legacy):
    queued = await db.queue.enqueue(
        session_name="ike", message=_SECRET, delivery_kind="direct_message"
    )
    async with db.queue._tx() as conn:
        await conn.execute(
            text("UPDATE message_queue SET enqueued_at = '2000-01-01T00:00:00+00:00'")
        )
        if legacy:
            await conn.execute(text("UPDATE message_queue SET operation_id = NULL"))
    expired = await db.queue.expire_pending()
    assert len(expired) == 1
    receipt = (await db.deliveries.query())[0]
    queued_row = await queue_row(db, queued.id)
    assert receipt["operation_id"] == queued_row["operation_id"]
    evidence = await db.diagnostics.query(operation_id=receipt["operation_id"])
    assert [row["code"] for row in evidence] == ["queue_expired"]
    assert evidence[0]["queue_id"] == queued.id
    assert evidence[0]["delivery_id"] == receipt["id"]
    assert _SECRET not in json.dumps(evidence)
    assert await db.queue.expire_pending() == []


@pytest.mark.parametrize(
    "reason", ["acknowledged", "no_repo", "issue_closed", "no_longer_targeted"]
)
async def test_intentional_queue_retirement_does_not_report_recovery(db, reason):
    queued = await db.queue.enqueue(session_name="ike", message=_SECRET, issue_number=7)
    await db.queue.dequeue("ike")
    await db.queue.mark_delivered(queued.id, reason=reason)
    await db.queue.mark_delivered(queued.id, reason=reason)
    evidence = await db.diagnostics.query(operation_id=queued.operation_id)
    assert [row["code"] for row in evidence] == [f"retired_{reason}"]
    assert evidence[0]["occurrences"] == 1
    assert evidence[0]["queue_id"] == queued.id
    assert evidence[0]["details"]["reason"] == reason


async def test_issue_close_records_only_matching_queue_retirements(db):
    first = await db.queue.enqueue(
        session_name="ike", message=_SECRET, issue_number=7, repo="example/one"
    )
    other = await db.queue.enqueue(
        session_name="ike", message=_SECRET, issue_number=7, repo="example/two"
    )
    assert await db.queue.purge_for_issue(7, repo="example/one") == 1
    assert [row["code"] for row in await db.diagnostics.query(operation_id=first.operation_id)] == [
        "retired_issue_closed"
    ]
    assert await db.diagnostics.query(operation_id=other.operation_id) == []
    assert (await queue_row(db, other.id))["status"] == "pending"


async def test_legacy_issue_retry_uses_its_exact_delivery_identity(config, db):
    from agent_backbone.models import IssueData, ParsedLabels

    first_id = await db.deliveries.record(
        repo="example/orchestration",
        issue_number=7,
        target_entity="ike",
        session_name="ike",
        outcome="offline",
    )
    async with db.deliveries._tx() as conn:
        await conn.execute(text("UPDATE deliveries SET operation_id = NULL"))
    original = (await db.deliveries.query())[0]
    gh = AsyncMock()
    gh.get_issue.return_value = IssueData(
        number=7, repo_full_name="example/orchestration", labels=ParsedLabels(targets=["ike"])
    )
    with (
        patch("agent_backbone.services.jobs.retry.list_open_queue_for_target", return_value=[]),
        patch(
            f"{_DELIVERY}.get_session_intelligence",
            return_value=profile(SessionIntelligence.AGENT_WORKING),
        ),
    ):
        await retry_delivery(config, original, db, gh)
        await retry_delivery(config, original, db, gh)
    evidence = await db.diagnostics.query()
    deferred = [row for row in evidence if row["code"] == "deferred_agent_working"]
    assert len(deferred) == 1 and deferred[0]["occurrences"] == 2
    latest = (await db.deliveries.query())[0]
    assert latest["id"] != first_id
    assert latest["operation_id"] == deferred[0]["operation_id"]


async def test_outbox_replay_and_queue_drain_keep_event_correlation(config, db):
    event_id = await db.events.record(
        delivery_id="comment:example/test:123", source="webhook", event_type="comment_created"
    )
    plan = [
        {
            "session_name": "ike",
            "target_entity": "ike",
            "message": _SECRET,
            "repo": "example/test",
            "issue_number": 7,
            "delivery_kind": "comment",
            "enforce_issue_queue": False,
            "source_key": "comment:example/test:123",
        }
    ]
    with (
        patch(
            f"{_DELIVERY}.get_session_intelligence",
            return_value=profile(SessionIntelligence.AGENT_WORKING),
        ) as state,
        patch(f"{_DELIVERY}.send_message", return_value=True) as send,
    ):
        with patch.object(db.queue, "enqueue", side_effect=RuntimeError("queue unavailable")):
            await flush_outbox(event_id, config, db, None, plan=plan)
        failed = next(
            row for row in await db.diagnostics.query() if row["code"] == "queue_storage_failed"
        )
        await retry_outbox(config, db, None)
        queue_id = (await db.diagnostics.query(operation_id=failed["operation_id"]))[0]["queue_id"]
        assert queue_id is not None
        await drain_message_queue(config, db, None, active_sessions={"ike"})
        deferred = next(
            row
            for row in await db.diagnostics.query(operation_id=failed["operation_id"])
            if row["code"] == "deferred_agent_working"
        )
        assert deferred["event_id"] == event_id
        assert deferred["queue_id"] == queue_id
        assert deferred["occurrences"] == 3
        state.return_value = profile()
        await drain_message_queue(config, db, None, active_sessions={"ike"})
        send.assert_awaited_once()
    evidence = await db.diagnostics.query(operation_id=failed["operation_id"])
    assert any(row["code"] == "submitted" and row["queue_id"] == queue_id for row in evidence)
    assert (await queue_row(db, queue_id))["operation_id"] == failed["operation_id"]
