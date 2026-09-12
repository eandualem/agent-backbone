"""Repository launches fail closed before terminal or trust side effects."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.config import AgentSpec, bootstrap_config
from agent_backbone.services.agents import AgentStore, start_agent
from agent_backbone.services.agents.operations import StartRequest, resolve_agent, start_resolved
from agent_backbone.services.runtimes import RUNTIMES

_LAUNCH = "agent_backbone.services.agents.launch"


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("swarm", [None, "audit"])
@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (True, None),
        (False, "actions_disabled"),
        (None, "actions_unverified"),
        (1, "actions_unverified"),
        ("true", "actions_unverified"),
        (RuntimeError("PRIVATE_TOKEN"), "actions_unverified"),
        (TimeoutError("PRIVATE_NETWORK_DETAIL"), "actions_unverified"),
    ],
)
async def test_gate_for_fresh_resumed_and_worker_starts(
    db, tmp_path, resume, swarm, answer, reason
):
    config = bootstrap_config(tmp_path / "data")
    spec = AgentSpec(
        name="app",
        dir=str(tmp_path),
        runtime="shell",
        repo="acme/app",
        tags=(f"swarm:{swarm}",) if swarm else (),
    )
    check = (
        AsyncMock(side_effect=answer)
        if isinstance(answer, Exception)
        else AsyncMock(return_value=answer)
    )
    with (
        patch(f"{_LAUNCH}.session_exists", AsyncMock(return_value=False)),
        patch(f"{_LAUNCH}.start_session", AsyncMock(return_value=True)) as start,
        patch.object(RUNTIMES["shell"], "pre_trust") as trust,
    ):
        result = await start_agent(
            spec, config, db=db, resume=resume, wait=False, check_actions=check
        )
    check.assert_awaited_once_with("acme/app")
    assert result.ok is (reason is None)
    if reason:
        start.assert_not_awaited()
        trust.assert_not_called()
        assert "acme/app" in result.evidence[0]
        assert not (config.state_dir / "app.starting").exists()
        failed = next(
            row for row in await db.diagnostics.query(agent_name="app") if row["code"] == "failed"
        )
        assert failed["details"]["stage"] == "preflight"
        assert failed["details"]["reason"] == reason
        serialized = json.dumps(failed) + str(result)
        assert "PRIVATE_" not in serialized
    else:
        start.assert_awaited_once()


@pytest.mark.parametrize(
    ("repo", "running", "ok"),
    [("", False, True), ("acme/app", True, True), ("acme/app", False, False)],
)
async def test_repo_less_noop_and_missing_checker(tmp_path, repo, running, ok):
    with (
        patch(f"{_LAUNCH}.session_exists", AsyncMock(return_value=running)),
        patch(f"{_LAUNCH}.start_session", AsyncMock(return_value=True)) as start,
    ):
        result = await start_agent(
            AgentSpec(name="app", dir=str(tmp_path), runtime="shell", repo=repo),
            bootstrap_config(tmp_path / "data"),
            wait=False,
        )
    assert result.ok is ok
    assert start.await_count == (not running and ok)


async def test_named_registration_uses_current_origin_and_ignores_watches(db, tmp_path):
    store = AgentStore(db, tmp_path / "data")
    await store.start()
    await store.register(
        AgentSpec(
            name="app",
            dir=str(tmp_path),
            runtime="shell",
            repo="old/repo",
            watches=("watched/repo",),
        )
    )
    req = StartRequest(name="app", resume=True, wait=False)
    spec = await resolve_agent(store, req)
    check = AsyncMock(return_value=False)
    with (
        patch(f"{_LAUNCH}.detect_repo", AsyncMock(return_value="actual/repo")),
        patch(f"{_LAUNCH}.session_exists", AsyncMock(return_value=False)),
        patch(f"{_LAUNCH}.start_session", AsyncMock()) as start,
    ):
        result = await start_resolved(store, store.config, spec, req, db=db, check_actions=check)
    assert not result.ok
    check.assert_awaited_once_with("actual/repo")
    start.assert_not_awaited()
