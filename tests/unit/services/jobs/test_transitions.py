"""The transitions job: stop now, start when due, hand over the message, tell the truth."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.config import AgentsConfig
from agent_backbone.models import DeliveryOutcome
from agent_backbone.services.agents import (
    AgentStore,
    StartResult,
    lifecycle_lock,
    write_starting_marker,
    write_state_file,
)
from agent_backbone.services.database import now_iso
from agent_backbone.services.jobs.retry import drain_message_queue
from agent_backbone.services.jobs.transitions import run_transitions
from agent_backbone.services.routing import DeliveryReport, safe_deliver
from agent_backbone.services.routing.models import SessionIntelligence, SessionProfile
from agent_backbone.services.runtimes import RUNTIMES

_JOB = "agent_backbone.services.jobs.transitions"
_DELIVERY = "agent_backbone.services.routing._delivery"
PAST = "2020-01-01T00:00:00.000000Z"
FUTURE = "2099-01-01T00:00:00.000000Z"


@pytest.fixture
async def store(db, config):
    for spec in config.agents:
        await db.agents.upsert(
            spec.name,
            dir=spec.dir,
            runtime=spec.runtime,
            model=spec.model,
            repo=spec.repo,
            tags=list(spec.tags),
            env=dict(spec.env),
            description=spec.description,
            always_on=spec.always_on,
            unattended=spec.unattended,
        )
    store = AgentStore(db, config.data_dir)
    await store.refresh()
    return store


@pytest.fixture
def seams():
    """No tmux and no real launch: the stop and the start are the seams."""
    with (
        patch(f"{_JOB}.launch.stop_agent", new_callable=AsyncMock, return_value=True) as stop,
        patch(
            f"{_JOB}.start_resolved",
            new_callable=AsyncMock,
            return_value=StartResult(ok=True, ready="ready", evidence=("hook reported idle",)),
        ) as start,
        patch(
            f"{_JOB}.safe_deliver",
            new_callable=AsyncMock,
            return_value=DeliveryReport(outcome=DeliveryOutcome.DELIVERED),
        ) as deliver,
        # A running session carries no launch of ours unless a test says so.
        patch(f"{_JOB}.query_environment_var", new_callable=AsyncMock, return_value=None),
    ):
        yield stop, start, deliver


async def _run(config, store, db):
    return await run_transitions(lambda: config, store, db)


async def test_stop_now_then_start_when_due(db, config, store, seams):
    stop, start, deliver = seams
    row = await db.transitions.create(agent_name="ike", delay_seconds=3600, message="carry on")

    assert await _run(config, store, db) == {"ike": "stopped"}
    stop.assert_awaited_once_with("ike")
    stopped = await db.transitions.get(row["id"])
    assert stopped["status"] == "pending" and stopped["stopped_at"] and stopped["start_at"]
    start.assert_not_awaited()

    assert await _run(config, store, db) == {"ike": "waiting"}  # not due yet
    start.assert_not_awaited()

    await db.transitions.mark_stopped(row["id"], start_at=PAST)  # no-op: already set
    async with db.engine.begin() as conn:
        from sqlalchemy import text

        await conn.execute(
            text("UPDATE agent_transitions SET start_at = :t WHERE id = :id"),
            {"t": PAST, "id": row["id"]},
        )
    assert await _run(config, store, db) == {"ike": "started"}
    start.assert_awaited_once()
    req = start.await_args.args[3]
    assert req.name == "ike" and req.resume is False and req.wait is True
    deliver.assert_awaited_once()
    assert deliver.await_args.args[0] == "ike"
    assert deliver.await_args.args[1] == "[via:backbone from:backbone] carry on"
    assert deliver.await_args.kwargs["source"] == "agent-restart"
    done = await db.transitions.get(row["id"])
    assert done["status"] == "completed"
    assert done["result"]["ready"] == "ready" and done["result"]["message"] == "delivered"
    assert await _run(config, store, db) == {}


async def test_default_delay_and_a_zero_delay_start_in_the_same_tick(db, config, store, seams):
    stop, start, _ = seams
    default = await db.transitions.create(agent_name="ike")
    assert default["delay_seconds"] == 0
    assert await _run(config, store, db) == {"ike": "started"}
    start.assert_awaited_once()

    await db.transitions.create(agent_name="leo", delay_seconds=0)
    assert await _run(config, store, db) == {"leo": "started"}
    assert stop.await_count == 2 and start.await_count == 2


async def test_stop_only_completes_after_the_stop(db, config, store, seams):
    stop, start, _ = seams
    row = await db.transitions.create(agent_name="ike", start=False)
    assert await _run(config, store, db) == {"ike": "stopped"}
    done = await db.transitions.get(row["id"])
    assert done["status"] == "completed" and done["result"] == {"stopped": True}
    start.assert_not_awaited()


@pytest.mark.parametrize("runtime", sorted(RUNTIMES))
async def test_impossible_resume_never_stops_existing_session(db, config, store, seams, runtime):
    stop, start, _ = seams
    spec = replace(next(iter(config.agents)), runtime=runtime)
    name = spec.name
    await store.update(name, runtime=runtime)
    config = replace(config, agents=AgentsConfig(specs={name: spec}))
    write_state_file(
        config.state_dir,
        name,
        {
            "state": "idle",
            "ts": time.time(),
            "runtime": runtime,
            "session_id": "old",
        },
    )
    # A replacement started after the request and has not reported its own identity.
    write_starting_marker(config.state_dir, name, time.time(), launch_id="replacement")
    row = await db.transitions.create(agent_name=name, resume=True)
    assert await _run(config, store, db) == {name: "failed"}
    stop.assert_not_awaited()
    start.assert_not_awaited()
    failed = await db.transitions.get(row["id"])
    assert failed["status"] == "failed" and failed["stopped_at"] is None
    assert "resum" in failed["result"]["reason"]


async def test_resume_rechecks_runtime_changed_while_waiting_for_lock(db, config, store, seams):
    stop, start, _ = seams
    name = next(iter(config.agents)).name
    write_state_file(
        config.state_dir,
        name,
        {
            "state": "idle",
            "ts": time.time(),
            "runtime": "claude",
            "session_id": "saved",
        },
    )
    row = await db.transitions.create(agent_name=name, resume=True)
    waiting = asyncio.Event()

    @asynccontextmanager
    async def observed_lock(agent_name):
        waiting.set()
        async with lifecycle_lock(agent_name):
            yield

    with patch(f"{_JOB}.lifecycle_lock", observed_lock):
        async with lifecycle_lock(name):
            task = asyncio.create_task(_run(config, store, db))
            await asyncio.wait_for(waiting.wait(), timeout=2)
            await store.update(name, runtime="aider")
        assert await task == {name: "failed"}
    stop.assert_not_awaited()
    start.assert_not_awaited()
    failed = await db.transitions.get(row["id"])
    assert failed["stopped_at"] is None
    assert "cannot resume an exact session" in failed["result"]["reason"]


async def test_runtime_model_and_resume_reach_the_start_request(db, config, store, seams):
    _, start, _ = seams
    write_state_file(
        config.state_dir,
        config.agents.get("ike").name,
        {
            "state": "idle",
            "ts": time.time(),
            "runtime": "claude",
            "session_id": "saved",
        },
    )
    with patch(f"{_JOB}.resolve_agent", new_callable=AsyncMock) as resolve:
        resolve.return_value = config.agents.get("ike")
        await db.transitions.create(
            agent_name="ike", runtime="claude", model="opus:high", resume=True, delay_seconds=0
        )
        await _run(config, store, db)
    req = resolve.await_args.args[1]
    assert (req.runtime, req.model, req.resume) == ("claude", "opus:high", True)
    assert start.await_args.args[3] is req


async def test_a_session_found_running_at_start_time_is_a_failure_not_a_claim(
    db, config, store, seams
):
    _, start, deliver = seams
    start.return_value = StartResult(ok=True, already_running=True)
    row = await db.transitions.create(agent_name="ike", delay_seconds=0, message="note")
    assert await _run(config, store, db) == {"ike": "failed"}
    done = await db.transitions.get(row["id"])
    assert done["status"] == "failed" and "already running" in done["result"]["reason"]
    deliver.assert_not_awaited()


async def test_a_launch_that_exits_or_raises_is_recorded_as_failed(db, config, store, seams):
    _, start, deliver = seams
    start.return_value = StartResult(ok=False, ready="exited", evidence=("crashed",))
    row = await db.transitions.create(agent_name="ike", delay_seconds=0)
    assert await _run(config, store, db) == {"ike": "failed"}
    done = await db.transitions.get(row["id"])
    assert done["result"]["reason"] == "the replacement did not start"
    assert done["result"]["evidence"] == ["crashed"]

    start.side_effect = ValueError("Runtime 'codex' binary not found")
    row = await db.transitions.create(agent_name="ike", delay_seconds=0)
    assert await _run(config, store, db) == {"ike": "failed"}
    assert (await db.transitions.get(row["id"]))["result"] == {
        "reason": "Runtime 'codex' binary not found"
    }
    deliver.assert_not_awaited()


async def test_a_failed_stop_is_reported_not_started(db, config, store, seams):
    stop, start, _ = seams
    stop.return_value = False
    row = await db.transitions.create(agent_name="ike", delay_seconds=0)
    assert await _run(config, store, db) == {"ike": "failed"}
    assert (await db.transitions.get(row["id"]))["result"] == {
        "reason": "could not stop the session"
    }
    start.assert_not_awaited()


async def test_a_row_stopped_before_a_backbone_restart_is_started_by_the_new_process(
    db, config, store, seams
):
    """The previous process stopped the session and died; the row carries on."""
    stop, start, _ = seams
    row = await db.transitions.create(agent_name="ike", requested_by="ike", message="hi")
    await db.transitions.mark_stopped(row["id"], start_at=PAST)
    assert await _run(config, store, db) == {"ike": "started"}
    stop.assert_not_awaited()
    start.assert_awaited_once()


async def test_a_stop_only_row_stopped_before_a_backbone_restart_never_starts(
    db, config, store, seams
):
    stop, start, _ = seams
    row = await db.transitions.create(agent_name="ike", start=False)
    await db.transitions.mark_stopped(row["id"], start_at=PAST)
    assert await _run(config, store, db) == {"ike": "stopped"}
    assert (await db.transitions.get(row["id"]))["status"] == "completed"
    stop.assert_not_awaited()
    start.assert_not_awaited()


async def test_the_launch_identity_is_persisted_before_the_start(db, config, store, seams):
    _, start, _ = seams
    row = await db.transitions.create(agent_name="ike", delay_seconds=0)
    await _run(config, store, db)
    req = start.await_args.args[3]
    assert (await db.transitions.get(row["id"]))["launch_operation_id"] == req.operation_id


async def test_a_launch_interrupted_by_a_backbone_restart_is_recovered_from_diagnostics(
    db, config, store, seams
):
    """The earlier process launched the replacement and died before closing the
    row; the new process finds the session running and the launch recorded."""
    _, start, deliver = seams
    start.return_value = StartResult(ok=True, already_running=True)
    row = await db.transitions.create(agent_name="ike", message="hi")
    await db.transitions.mark_stopped(row["id"], start_at=PAST)
    await db.transitions.mark_launching(row["id"], "op-9")
    for code in ("requested", "ready"):
        await db.diagnostics.record(
            category="startup", operation_id="op-9", code=code, severity="info", agent_name="ike"
        )
    with patch(f"{_JOB}.query_environment_var", new_callable=AsyncMock, return_value="op-9"):
        assert await _run(config, store, db) == {"ike": "started"}
    assert start.await_args.args[3].operation_id == "op-9"
    done = await db.transitions.get(row["id"])
    assert done["status"] == "completed" and done["result"]["ready"] == "ready"
    assert "earlier backbone process" in done["result"]["evidence"][0]
    deliver.assert_awaited_once()


@pytest.fixture
def terminal():
    """The real delivery behind the ``safe_deliver`` seam, into an agent at its
    prompt; the paste itself is counted."""
    with (
        patch(
            f"{_DELIVERY}.get_session_intelligence",
            new_callable=AsyncMock,
            return_value=_profile(SessionIntelligence.READY),
        ) as readiness,
        patch(f"{_DELIVERY}.send_message", new_callable=AsyncMock, return_value=True) as paste,
    ):
        yield readiness, paste


def _profile(condition):
    return SessionProfile(session_name="ike", intelligence=condition, runtime="shell")


def _stops_after_first(deliver):
    """The real delivery; the service stops right after the first one, before
    the transition is closed."""

    async def deliver_then_stop(*args, **kwargs):
        report = await safe_deliver(*args, **kwargs)
        if deliver.await_count == 1:
            raise asyncio.CancelledError
        return report

    return deliver_then_stop


async def _recover(config, store, db, start, row_id):
    """The next process finds the replacement this transition launched running."""
    launch_op = (await db.transitions.get(row_id))["launch_operation_id"]
    await db.diagnostics.record(
        category="startup", operation_id=launch_op, code="ready", agent_name="ike"
    )
    start.side_effect = None
    start.return_value = StartResult(ok=True, already_running=True)
    with patch(f"{_JOB}.query_environment_var", new_callable=AsyncMock, return_value=launch_op):
        return await _run(config, store, db)


async def test_a_continuation_pasted_before_a_backbone_restart_is_not_pasted_again(
    db, config, store, seams, terminal
):
    """The earlier process launched the replacement, pasted the continuation
    and exited before closing the row: the next one finds the paste recorded."""
    _, start, deliver = seams
    _, paste = terminal
    deliver.side_effect = _stops_after_first(deliver)
    row = await db.transitions.create(agent_name="ike", delay_seconds=0, message="hi")
    with pytest.raises(asyncio.CancelledError):
        await _run(config, store, db)

    assert await _recover(config, store, db, start, row["id"]) == {"ike": "started"}
    paste.assert_awaited_once()  # never pasted a second time
    done = await db.transitions.get(row["id"])
    assert done["status"] == "completed" and done["result"]["message"] == "already_delivered"


async def test_a_paste_whose_submitted_record_was_lost_is_not_pasted_again(
    db, config, store, seams, terminal
):
    """The service stopped between the delivery receipt and its diagnostic:
    the saved receipt is enough for the next process."""
    _, start, deliver = seams
    _, paste = terminal
    deliver.side_effect = safe_deliver
    record = db.diagnostics.record

    async def stop_at_submitted(**kwargs):
        if kwargs["code"] == "submitted":
            raise asyncio.CancelledError
        return await record(**kwargs)

    row = await db.transitions.create(agent_name="ike", delay_seconds=0, message="hi")
    with (
        patch.object(db.diagnostics, "record", side_effect=stop_at_submitted),
        pytest.raises(asyncio.CancelledError),
    ):
        await _run(config, store, db)

    assert await _recover(config, store, db, start, row["id"]) == {"ike": "started"}
    paste.assert_awaited_once()


async def test_a_queued_copy_the_drain_pastes_during_recovery_is_not_pasted_again(
    db, config, store, seams, terminal
):
    """The earlier process queued the continuation for a busy agent and exited.
    On recovery the queue drain pastes that copy after the transition looked
    and before its delivery takes the session: the delivery still finds it."""
    _, start, deliver = seams
    readiness, paste = terminal
    readiness.return_value = _profile(SessionIntelligence.AGENT_WORKING)
    deliver.side_effect = _stops_after_first(deliver)
    row = await db.transitions.create(agent_name="ike", delay_seconds=0, message="hi")
    with pytest.raises(asyncio.CancelledError):
        await _run(config, store, db)
    paste.assert_not_awaited()

    readiness.return_value = _profile(SessionIntelligence.READY)

    async def drain_first(*args, **kwargs):
        await drain_message_queue(config, db, AsyncMock(), active_sessions={"ike"})
        return await safe_deliver(*args, **kwargs)

    deliver.side_effect = drain_first
    assert await _recover(config, store, db, start, row["id"]) == {"ike": "started"}
    paste.assert_awaited_once()


async def test_a_later_replacement_gets_the_continuation_an_earlier_one_received(
    db, config, store, seams, terminal
):
    """The first replacement got the continuation; the backbone stopped before
    closing the row and that session exited. A later process launched a fresh
    one and stopped before sending: that conversation never saw the message."""
    _, start, deliver = seams
    _, paste = terminal

    async def fresh_launch(*args, **kwargs):
        await db.agents.conversation_started("ike", now_iso())
        return StartResult(ok=True, ready="ready")

    async def first_only_then_stop(*args, **kwargs):
        if deliver.await_count == 1:
            await safe_deliver(*args, **kwargs)
        raise asyncio.CancelledError

    start.side_effect = fresh_launch
    deliver.side_effect = first_only_then_stop
    row = await db.transitions.create(agent_name="ike", delay_seconds=0, message="hi")
    for _ in range(2):
        with pytest.raises(asyncio.CancelledError):
            await _run(config, store, db)
    paste.assert_awaited_once()

    deliver.side_effect = safe_deliver
    assert await _recover(config, store, db, start, row["id"]) == {"ike": "started"}
    assert paste.await_count == 2
    done = await db.transitions.get(row["id"])
    assert done["status"] == "completed" and done["result"]["message"] == "delivered"


async def test_the_retrys_own_already_running_record_does_not_hide_the_launch(
    db, config, store, seams
):
    """The retry records "already_running" under the same operation: the
    earlier launch's outcome is read before it."""
    _, start, _ = seams

    async def retry(store, config, spec, req, db):
        await db.diagnostics.record(
            category="startup",
            operation_id=req.operation_id,
            code="already_running",
            severity="info",
            agent_name="ike",
        )
        return StartResult(ok=True, already_running=True)

    start.side_effect = retry
    row = await db.transitions.create(agent_name="ike")
    await db.transitions.mark_stopped(row["id"], start_at=PAST)
    await db.transitions.mark_launching(row["id"], "op-11")
    await db.diagnostics.record(
        category="startup", operation_id="op-11", code="ready", severity="info", agent_name="ike"
    )
    with patch(f"{_JOB}.query_environment_var", new_callable=AsyncMock, return_value="op-11"):
        assert await _run(config, store, db) == {"ike": "started"}


async def test_an_interrupted_retry_does_not_hide_the_launch_from_the_next(
    db, config, store, seams
):
    """A first retry recorded "already_running" and died before closing the
    row: the original launch's outcome is still the one that counts."""
    _, start, _ = seams
    start.return_value = StartResult(ok=True, already_running=True)
    row = await db.transitions.create(agent_name="ike")
    await db.transitions.mark_stopped(row["id"], start_at=PAST)
    await db.transitions.mark_launching(row["id"], "op-12")
    for code in ("requested", "ready", "already_running"):
        await db.diagnostics.record(
            category="startup", operation_id="op-12", code=code, severity="info", agent_name="ike"
        )
    with patch(f"{_JOB}.query_environment_var", new_callable=AsyncMock, return_value="op-12"):
        assert await _run(config, store, db) == {"ike": "started"}


async def test_a_later_successful_launch_counts_over_an_earlier_failure(db, config, store, seams):
    """A failed launch, then a retry that launched the replacement and died
    before closing the row: the running replacement is this transition's."""
    _, start, _ = seams
    start.return_value = StartResult(ok=True, already_running=True)
    row = await db.transitions.create(agent_name="ike")
    await db.transitions.mark_stopped(row["id"], start_at=PAST)
    await db.transitions.mark_launching(row["id"], "op-13")
    for code in ("requested", "failed", "requested", "ready"):
        await db.diagnostics.record(
            category="startup", operation_id="op-13", code=code, severity="info", agent_name="ike"
        )
    with patch(f"{_JOB}.query_environment_var", new_callable=AsyncMock, return_value="op-13"):
        assert await _run(config, store, db) == {"ike": "started"}


async def test_a_launch_that_only_found_a_running_session_is_not_claimed(db, config, store, seams):
    _, start, _ = seams
    start.return_value = StartResult(ok=True, already_running=True)
    row = await db.transitions.create(agent_name="ike")
    await db.transitions.mark_stopped(row["id"], start_at=PAST)
    await db.transitions.mark_launching(row["id"], "op-14")
    for code in ("requested", "already_running"):
        await db.diagnostics.record(
            category="startup", operation_id="op-14", code=code, severity="info", agent_name="ike"
        )
    assert await _run(config, store, db) == {"ike": "failed"}


async def test_a_session_someone_else_started_under_the_name_is_not_claimed(
    db, config, store, seams
):
    """The launched session exited and another was started under the same
    name: it carries another launch's operation id, so the continuation waits."""
    _, start, deliver = seams
    start.return_value = StartResult(ok=True, already_running=True)
    row = await db.transitions.create(agent_name="ike", message="hi")
    await db.transitions.mark_stopped(row["id"], start_at=PAST)
    await db.transitions.mark_launching(row["id"], "op-15")
    for code in ("requested", "ready"):
        await db.diagnostics.record(
            category="startup", operation_id="op-15", code=code, severity="info", agent_name="ike"
        )
    with patch(f"{_JOB}.query_environment_var", new_callable=AsyncMock, return_value="op-other"):
        assert await _run(config, store, db) == {"ike": "failed"}
    deliver.assert_not_awaited()


async def test_a_replacement_from_an_interrupted_retry_is_claimed_by_its_operation(
    db, config, store, seams
):
    """A retry launched another replacement under the same operation and was
    interrupted before recording its readiness: the session carries the id."""
    _, start, _ = seams
    start.return_value = StartResult(ok=True, already_running=True)
    row = await db.transitions.create(agent_name="ike")
    await db.transitions.mark_stopped(row["id"], start_at=PAST)
    await db.transitions.mark_launching(row["id"], "op-16")
    for code in ("requested", "ready", "requested"):
        await db.diagnostics.record(
            category="startup", operation_id="op-16", code=code, severity="info", agent_name="ike"
        )
    with patch(f"{_JOB}.query_environment_var", new_callable=AsyncMock, return_value="op-16"):
        assert await _run(config, store, db) == {"ike": "started"}


async def test_the_latest_observed_outcome_counts_whatever_its_row_order(db, config, store, seams):
    """ready, timeout, ready: the repeat updates the first row, so id order
    would pick the timeout."""
    _, start, _ = seams
    start.return_value = StartResult(ok=True, already_running=True)
    row = await db.transitions.create(agent_name="ike")
    await db.transitions.mark_stopped(row["id"], start_at=PAST)
    await db.transitions.mark_launching(row["id"], "op-17")
    for code in ("requested", "ready", "timeout", "ready"):
        await db.diagnostics.record(
            category="startup", operation_id="op-17", code=code, severity="info", agent_name="ike"
        )
    with patch(f"{_JOB}.query_environment_var", new_callable=AsyncMock, return_value="op-17"):
        assert await _run(config, store, db) == {"ike": "started"}
    assert (await db.transitions.get(row["id"]))["result"]["ready"] == "ready"


async def test_a_running_session_without_a_recorded_launch_is_not_claimed(db, config, store, seams):
    _, start, _ = seams
    start.return_value = StartResult(ok=True, already_running=True)
    row = await db.transitions.create(agent_name="ike")
    await db.transitions.mark_stopped(row["id"], start_at=PAST)
    await db.transitions.mark_launching(row["id"], "op-10")  # launch never got recorded
    assert await _run(config, store, db) == {"ike": "failed"}
    assert "already running" in (await db.transitions.get(row["id"]))["result"]["reason"]


async def test_a_continuation_that_cannot_be_stored_fails_the_transition(db, config, store, seams):
    _, _, deliver = seams
    deliver.return_value = DeliveryReport(outcome=DeliveryOutcome.OFFLINE, queue="failed")
    row = await db.transitions.create(agent_name="ike", delay_seconds=0, message="hi")
    assert await _run(config, store, db) == {"ike": "failed"}
    result = (await db.transitions.get(row["id"]))["result"]
    assert "neither delivered nor stored" in result["reason"]
    assert result["ready"] == "ready" and result["message_queue"] == "failed"


async def test_a_queued_message_is_reported_as_queued(db, config, store, seams):
    _, _, deliver = seams
    deliver.return_value = DeliveryReport(outcome=DeliveryOutcome.SETTLING, queue="stored")
    row = await db.transitions.create(agent_name="ike", delay_seconds=0, message="hi")
    await _run(config, store, db)
    result = (await db.transitions.get(row["id"]))["result"]
    assert result["message"] == "settling" and result["message_queue"] == "stored"


async def test_a_forgotten_agent_fails_cleanly(db, config, store, seams):
    stop, _, _ = seams
    row = await db.transitions.create(agent_name="nobody", delay_seconds=0)
    assert await _run(config, store, db) == {"nobody": "failed"}
    assert (await db.transitions.get(row["id"]))["result"] == {"reason": "agent is no longer known"}
    stop.assert_not_awaited()


async def test_an_unexpected_error_fails_the_row_and_continues(db, config, store, seams):
    stop, _, _ = seams
    stop.side_effect = [RuntimeError("tmux exploded"), True]
    first = await db.transitions.create(agent_name="ike", delay_seconds=0)
    second = await db.transitions.create(agent_name="leo", start=False)
    assert await _run(config, store, db) == {"ike": "failed", "leo": "stopped"}
    assert (await db.transitions.get(first["id"]))["result"] == {
        "reason": "unexpected error: RuntimeError"
    }
    assert (await db.transitions.get(second["id"]))["status"] == "completed"
