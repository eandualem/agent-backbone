"""Every start surface shares metadata-only attempt and outcome recording."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.config import AgentSpec, bootstrap_config
from agent_backbone.services.agents import AgentStore, start_agent
from agent_backbone.services.agents.operations import StartRequest, resolve_agent, start_resolved
from agent_backbone.services.runtimes import RUNTIMES

_LAUNCH = "agent_backbone.services.agents.launch"
PANE = (Path(__file__).parents[3] / "fixtures" / "codex-model-error.txt").read_text()


@pytest.mark.parametrize(
    ("ready", "severity"),
    [
        ("ready", "info"),
        ("waiting_for_human", "info"),
        ("exited", "error"),
        ("timeout", "warning"),
    ],
)
async def test_start_records_outcome_and_never_copies_human_evidence(db, tmp_path, ready, severity):
    spec = AgentSpec(name="app", dir=str(tmp_path), runtime="shell")
    config = bootstrap_config(tmp_path / "data")
    with (
        patch(f"{_LAUNCH}.session_exists", AsyncMock(return_value=False)),
        patch(f"{_LAUNCH}.start_session", AsyncMock(return_value=True)),
        patch(
            f"{_LAUNCH}.wait_until_ready",
            AsyncMock(return_value=(ready, ["PRIVATE_TERMINAL_TEXT"])),
        ),
    ):
        result = await start_agent(spec, config, db=db, operation_id="start-example")
    records = await db.diagnostics.query(operation_id="start-example")
    assert {row["code"] for row in records} == {"requested", ready}
    finished = next(row for row in records if row["code"] == ready)
    requested = next(row for row in records if row["code"] == "requested")
    assert finished["severity"] == severity
    assert finished["details"]["stage"] == "readiness"
    assert requested["details"]["stage"] == "preflight"
    assert "duration_ms" in finished["details"]
    assert result.evidence == ("PRIVATE_TERMINAL_TEXT",)
    assert "PRIVATE_TERMINAL_TEXT" not in json.dumps(records)
    assert str(tmp_path) not in json.dumps(records)


@pytest.mark.parametrize(("running", "code"), [(True, "already_running"), (False, "not_waited")])
async def test_existing_and_unwaited_sessions_do_not_claim_readiness(db, tmp_path, running, code):
    with (
        patch(f"{_LAUNCH}.session_exists", AsyncMock(return_value=running)),
        patch(f"{_LAUNCH}.start_session", AsyncMock(return_value=True)),
    ):
        await start_agent(
            AgentSpec(name="app", dir=str(tmp_path), runtime="shell"),
            bootstrap_config(tmp_path / "data"),
            db=db,
            wait=False,
        )
    records = await db.diagnostics.query(agent_name="app")
    assert {row["code"] for row in records} == {"requested", code}


async def test_unexpected_launch_error_is_recorded_and_original_error_preserved(db, tmp_path):
    error = OSError("PRIVATE_EXCEPTION_DETAIL")
    with (
        patch(f"{_LAUNCH}.session_exists", AsyncMock(return_value=False)),
        patch(f"{_LAUNCH}.start_session", AsyncMock(side_effect=error)),
        pytest.raises(OSError, match="PRIVATE_EXCEPTION_DETAIL"),
    ):
        await start_agent(
            AgentSpec(name="app", dir=str(tmp_path), runtime="shell"),
            bootstrap_config(tmp_path / "data"),
            db=db,
        )
    records = await db.diagnostics.query(agent_name="app")
    failed = next(row for row in records if row["code"] == "failed")
    assert failed["details"]["stage"] == "launch"
    assert failed["details"]["error_type"] == "OSError"
    assert "PRIVATE_EXCEPTION_DETAIL" not in json.dumps(records)


async def test_rejected_resolution_is_durable(db, tmp_path):
    store = AgentStore(db, tmp_path)
    await store.start()
    req = StartRequest(name="missing")
    with pytest.raises(KeyError):
        await resolve_agent(store, req)
    records = await db.diagnostics.query(operation_id=req.operation_id)
    assert len(records) == 1 and records[0]["code"] == "failed"
    assert records[0]["details"]["stage"] == "resolve"


async def test_preflight_failure_is_recorded_without_attempting_launch(db, tmp_path):
    store = AgentStore(db, tmp_path)
    await store.start()
    await store.register(AgentSpec(name="app", dir=str(tmp_path), runtime="codex"))
    req = StartRequest(name="app")
    spec = await resolve_agent(store, req)
    with (
        patch.object(RUNTIMES["codex"], "available", return_value=False),
        patch(f"{_LAUNCH}.start_session", AsyncMock()) as start,
        pytest.raises(ValueError, match="binary not found"),
    ):
        await start_resolved(store, store.config, spec, req, db=db)
    start.assert_not_awaited()
    records = await db.diagnostics.query(operation_id=req.operation_id)
    assert len(records) == 1 and records[0]["details"]["stage"] == "preflight"


async def test_launch_value_error_is_not_reclassified_as_a_preflight_failure(db, tmp_path):
    store = AgentStore(db, tmp_path)
    await store.start()
    await store.register(AgentSpec(name="app", dir=str(tmp_path), runtime="shell"))
    req = StartRequest(name="app", wait=False)
    spec = await resolve_agent(store, req)
    with (
        patch(f"{_LAUNCH}.session_exists", AsyncMock(return_value=False)),
        patch(f"{_LAUNCH}.start_session", AsyncMock(side_effect=ValueError("private failure"))),
        pytest.raises(ValueError, match="private failure"),
    ):
        await start_resolved(store, store.config, spec, req, db=db)
    records = await db.diagnostics.query(operation_id=req.operation_id)
    failures = [row for row in records if row["code"] == "failed"]
    assert len(failures) == 1
    assert failures[0]["details"]["stage"] == "launch"
    assert failures[0]["occurrences"] == 1


async def test_start_captures_typed_error_and_model_change_before_returning(db, tmp_path):
    config = bootstrap_config(tmp_path / "data")
    with (
        patch(f"{_LAUNCH}.session_exists", AsyncMock(side_effect=[False, True])),
        patch(f"{_LAUNCH}.start_session", AsyncMock(return_value=True)),
        patch(f"{_LAUNCH}.capture_pane", AsyncMock(return_value=PANE)),
        patch(f"{_LAUNCH}.read_state_file", return_value=None),
        patch("agent_backbone.services.runtimes.base.resolve_command", return_value="/bin/codex"),
        patch.object(RUNTIMES["codex"], "pre_trust"),
    ):
        result = await start_agent(
            AgentSpec(name="app", dir=str(tmp_path), runtime="codex", model="example-model"),
            config,
            db=db,
            operation_id="observed-start",
        )
    # The existing readiness decision is preserved. Runtime error observations
    # survive independently of a later prompt or model-change notice.
    assert result.ready == "ready"
    records = await db.diagnostics.query(operation_id="observed-start")
    assert {row["code"] for row in records} == {
        "requested",
        "model_account_incompatible",
        "model_changed",
        "ready",
    }
    failure = next(row for row in records if row["code"] == "model_account_incompatible")
    assert failure["model"] == "example-model"
    assert failure["details"]["http_status"] == 400
    assert failure["details"]["model_source"] == "terminal"
    serialized = json.dumps(records)
    assert "ChatGPT account" not in serialized and '"message"' not in serialized
    assert str(tmp_path) not in serialized


async def test_repeated_visibility_and_a_second_model_keep_distinct_metadata(db, tmp_path):
    async def ready(*args, observe, **kwargs):
        await observe("• Model changed to example-a high")
        await observe("• Model changed to example-a high")
        await observe("• Model changed to example-b low")
        return "ready", []

    with (
        patch(f"{_LAUNCH}.session_exists", AsyncMock(return_value=False)),
        patch(f"{_LAUNCH}.start_session", AsyncMock(return_value=True)),
        patch(f"{_LAUNCH}.wait_until_ready", side_effect=ready),
        patch("agent_backbone.services.runtimes.base.resolve_command", return_value="/bin/codex"),
        patch.object(RUNTIMES["codex"], "pre_trust"),
    ):
        await start_agent(
            AgentSpec(name="app", dir=str(tmp_path), runtime="codex"),
            bootstrap_config(tmp_path / "data"),
            db=db,
            operation_id="one-start",
        )
    records = await db.diagnostics.query(operation_id="one-start", category="runtime")
    assert len(records) == 2
    assert {row["model"]: row["occurrences"] for row in records} == {"example-a": 2, "example-b": 1}
