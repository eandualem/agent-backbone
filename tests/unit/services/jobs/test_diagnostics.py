"""Job liveness cannot stand in for a failed sub-operation's recovery."""

from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.services.agents import AgentState, StateSnapshot
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.jobs.diagnostics import observe_job, observe_runtime
from agent_backbone.services.runtimes import RuntimeDiagnostic


async def test_only_exact_success_recovers_a_coalesced_failure():
    async with BackboneDB.connect() as db:
        await observe_job(db, source="github-poll", stage="repository", repo="acme/a")
        assert await db.diagnostics.query() == []
        for _ in range(2):
            await observe_job(
                db,
                source="github-poll",
                stage="repository",
                repo="acme/a",
                error_type="TimeoutError",
            )
        await observe_job(db, source="github-poll", stage="repository", repo="acme/b")
        await observe_job(db, source="github-poll", stage="run")
        (failed,) = await db.diagnostics.query()
        assert failed["occurrences"] == 2
        await observe_job(db, source="github-poll", stage="repository", repo="acme/a")
        recovered, failure = await db.diagnostics.query()
        assert recovered["code"] == "repository_recovered"
        assert recovered["operation_id"] == failure["operation_id"]
        await observe_job(db, source="github-poll", stage="repository", repo="acme/a")
        assert len(await db.diagnostics.query()) == 2


async def test_next_failure_after_recovery_is_a_new_episode():
    async with BackboneDB.connect() as db:
        await observe_job(
            db,
            source="agent-monitor",
            stage="state_persistence",
            agent_name="a",
            error_type="OSError",
        )
        await observe_job(db, source="agent-monitor", stage="state_persistence", agent_name="a")
        await observe_job(
            db,
            source="agent-monitor",
            stage="state_persistence",
            agent_name="a",
            error_type="OSError",
        )
        newest, recovered, first = await db.diagnostics.query()
        assert recovered["operation_id"] == first["operation_id"]
        assert newest["operation_id"] != first["operation_id"]


async def test_runtime_error_repeats_and_absence_never_claims_recovery(db, config):
    error = RuntimeDiagnostic(
        code="request_error",
        error_type="invalid_request_error",
        http_status=400,
        model="model-x",
    )
    snapshot = StateSnapshot(
        state=AgentState.BUSY,
        runtime="codex",
        session_id="private-session-id",
        diagnostics=(error,),
        diagnostics_observed=True,
    )
    for _ in range(2):
        await observe_runtime(db, config, {"ike": snapshot})
    (row,) = await db.diagnostics.query()
    assert row["occurrences"] == 2
    assert row["details"]["http_status"] == 400
    assert row["details"]["state"] == "busy"
    assert row["runtime"] == "codex"
    assert snapshot.state == AgentState.BUSY
    assert "private-session-id" not in str(row)
    snapshot.diagnostics = ()
    snapshot.diagnostics_observed = False
    await observe_runtime(db, config, {"ike": snapshot})
    assert len(await db.diagnostics.query()) == 1  # missing capture proves nothing
    snapshot.diagnostics_observed = True
    for _ in range(2):
        await observe_runtime(db, config, {"ike": snapshot})
    absent, failure = await db.diagnostics.query()
    assert absent["code"] == "request_error_no_longer_visible"
    assert absent["occurrences"] == 1
    assert absent["operation_id"] == failure["operation_id"]
    assert absent["details"]["observed_model"] == "model-x"
    assert absent["details"]["http_status"] == 400
    snapshot.diagnostics = (error,)
    await observe_runtime(db, config, {"ike": snapshot})
    new = (await db.diagnostics.query())[0]
    assert new["operation_id"] != failure["operation_id"]


async def test_model_change_is_separate_evidence_from_error(db, config):
    snapshot = StateSnapshot(
        state=AgentState.IDLE,
        diagnostics_observed=True,
        diagnostics=(
            RuntimeDiagnostic(code="model_account_incompatible", model="model-x", http_status=400),
            RuntimeDiagnostic(code="model_changed", severity="info", model="model-y"),
        ),
    )
    await observe_runtime(db, config, {"ike": snapshot})
    rows = await db.diagnostics.query()
    assert {row["code"] for row in rows} == {"model_account_incompatible", "model_changed"}
    assert len({row["operation_id"] for row in rows}) == 1
    assert {row["details"]["observed_model"] for row in rows} == {"model-x", "model-y"}


async def test_usage_failure_recovery_is_scoped_to_runtime_and_stage(db):
    scope = dict(source="agent-monitor", stage="usage_registration", agent_name="worker")
    for _ in range(2):
        await observe_job(db, **scope, runtime="claude", error_type="ValueError")
    await observe_job(db, **scope, runtime="codex")
    await observe_job(
        db, source="agent-monitor", stage="usage_source", agent_name="worker", runtime="claude"
    )
    (failure,) = await db.diagnostics.query()
    assert failure["occurrences"] == 2
    assert failure["runtime"] == "claude"
    await observe_job(db, **scope, runtime="claude")
    recovered, failure = await db.diagnostics.query()
    assert recovered["code"] == "usage_registration_recovered"
    assert recovered["operation_id"] == failure["operation_id"]


@pytest.mark.parametrize("capture", ["off", "write_failure"])
async def test_observed_success_ends_episode_without_a_recovery_record(db, capture):
    scope = {"source": "agent-monitor", "stage": "state_persistence", "agent_name": "worker"}
    await observe_job(db, **scope, error_type="OSError")
    if capture == "off":
        await db.settings.set("diagnostics.mode", "off")
        await observe_job(db, **scope)
        await db.settings.set("diagnostics.mode", "standard")
    else:
        with patch.object(db.diagnostics, "record", AsyncMock(return_value=None)):
            await observe_job(db, **scope)
    (first,) = await db.diagnostics.query()
    assert first["occurrences"] == 1
    await observe_job(db, **scope, error_type="OSError")
    newest, unchanged = await db.diagnostics.query()
    assert unchanged == first
    assert newest["operation_id"] != first["operation_id"]
    assert newest["occurrences"] == 1
    assert {newest["code"], first["code"]} == {"state_persistence_failed"}


async def test_off_period_runtime_signals_are_not_reported_after_capture_resumes(db, config):
    snapshot = StateSnapshot(
        state=AgentState.BUSY,
        diagnostics_observed=True,
        diagnostics=(
            RuntimeDiagnostic(code="request_error", model="disabled-model", http_status=400),
        ),
    )
    await db.settings.set("diagnostics.mode", "off")
    await observe_runtime(db, config, {"ike": snapshot})  # config still has its old mode
    assert await db.diagnostics.query() == []
    await db.settings.set("diagnostics.mode", "standard")
    snapshot.diagnostics = ()
    await observe_runtime(db, config, {"ike": snapshot})
    assert await db.diagnostics.query() == []


async def test_off_period_job_failure_does_not_create_a_later_recovery(db):
    await db.settings.set("diagnostics.mode", "off")
    await observe_job(db, source="prune", stage="run", error_type="OSError")
    await db.settings.set("diagnostics.mode", "standard")
    await observe_job(db, source="prune", stage="run")
    assert await db.diagnostics.query() == []


async def test_mode_change_during_runtime_capture_does_not_keep_signals(db, config):
    record = db.diagnostics.record

    async def disable_then_record(**values):
        await db.settings.set("diagnostics.mode", "off")
        return await record(**values)

    snapshot = StateSnapshot(
        state=AgentState.BUSY,
        diagnostics_observed=True,
        diagnostics=(
            RuntimeDiagnostic(code="request_error", model="disabled-model", http_status=400),
        ),
    )
    with patch.object(db.diagnostics, "record", side_effect=disable_then_record):
        await observe_runtime(db, config, {"ike": snapshot})
    await db.settings.set("diagnostics.mode", "standard")
    snapshot.diagnostics = ()
    await observe_runtime(db, config, {"ike": snapshot})
    assert await db.diagnostics.query() == []
