"""Job liveness cannot stand in for a failed sub-operation's recovery."""

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
