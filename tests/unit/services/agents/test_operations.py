"""The mutations every surface shares: stop and forget carry their guards here."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.config import AgentSpec
from agent_backbone.services.agents import AgentStore
from agent_backbone.services.agents.launch import StartResult
from agent_backbone.services.agents.operations import (
    StartRequest,
    forget_agent,
    resolve_agent,
    start_resolved,
    stop_agent_session,
)

_OPS = "agent_backbone.services.agents.operations"


async def test_stop_refuses_the_backbones_own_session(config):
    with patch(f"{_OPS}.launch.stop_agent", new_callable=AsyncMock) as stop:
        with pytest.raises(ValueError):
            await stop_agent_session(config, config.backbone.session_name)
    stop.assert_not_called()


async def test_stop_stops_an_agent(config):
    with patch(f"{_OPS}.launch.stop_agent", new_callable=AsyncMock, return_value=True) as stop:
        assert await stop_agent_session(config, "ike") is True
    stop.assert_awaited_once_with("ike")


async def test_forget_refuses_a_running_agent():
    store = AsyncMock()
    with patch(f"{_OPS}.session_exists", new_callable=AsyncMock, return_value=True):
        with pytest.raises(RuntimeError):
            await forget_agent(store, "ike")
    store.forget.assert_not_called()


async def registered_store(db, tmp_path):
    store = AgentStore(db, tmp_path)
    await store.start()
    await store.register(spec=AgentSpec(name="app", dir=str(tmp_path), runtime="shell"))
    return store


async def test_start_tags_a_new_agent_before_it_launches(db, tmp_path):
    store = AgentStore(db, tmp_path)
    await store.start()
    project = tmp_path / "site"
    project.mkdir()
    with patch("agent_backbone.services.agents.store.detect_repo", AsyncMock(return_value="")):
        spec = await resolve_agent(
            store, StartRequest(directory=str(project), tags=("coder", "web"))
        )
    assert spec.tags == ("coder", "web")
    again = await resolve_agent(store, StartRequest(name="site", tags=("web", "python")))
    assert again.tags == ("coder", "web", "python")


async def test_start_refuses_a_bad_tag_before_registering(db, tmp_path):
    store = AgentStore(db, tmp_path)
    await store.start()
    project = tmp_path / "site"
    project.mkdir()
    with pytest.raises(ValueError):
        await resolve_agent(store, StartRequest(directory=str(project), tags=("swarm:x",)))
    assert store.agents.get("site") is None


@pytest.mark.parametrize("mutation", ["forget", "update", "replace"])
@pytest.mark.parametrize("separate_store", [False, True])
async def test_start_rejects_a_spec_changed_after_resolution(
    db, tmp_path, mutation, separate_store
):
    store = await registered_store(db, tmp_path)
    req = StartRequest(name="app")
    spec = await resolve_agent(store, req)
    writer = AgentStore(db, tmp_path) if separate_store else store
    await writer.start()
    if mutation in {"forget", "replace"}:
        with patch("agent_backbone.services.terminal.session_exists", return_value=False):
            await writer.forget("app")
        if mutation == "replace":
            await writer.register(spec)
    else:
        await writer.update("app", description="changed after resolution")
    with patch(f"{_OPS}.launch.start_agent", AsyncMock()) as launch:
        with pytest.raises(ValueError, match="before startup"):
            await start_resolved(store, store.config, spec, req, db=db)
    launch.assert_not_awaited()
    assert (store.agents.get("app") is None) == (mutation == "forget")


async def test_start_rejects_registration_replaced_during_diagnostics(db, tmp_path):
    store = await registered_store(db, tmp_path)
    req = StartRequest(name="app", wait=False)
    spec = await resolve_agent(store, req)
    original_record = db.diagnostics.record
    original_profile = tmp_path / "agent-credentials"

    async def replace_after_resolution(**event):
        if event["code"] == "requested":
            await db.agents.delete("app")
            await db.agents.upsert(
                "app",
                dir=str(tmp_path),
                runtime="shell",
                model=None,
                repo="",
                tags=[],
                env={},
                description="",
                unattended=False,
            )
        await original_record(**event)

    with (
        patch.object(db.diagnostics, "record", side_effect=replace_after_resolution),
        patch("agent_backbone.services.agents.launch.session_exists", return_value=False),
        patch("agent_backbone.services.agents.launch.start_session") as launch,
        patch("agent_backbone.services.agents.launch.write_starting_marker") as marker,
    ):
        result = await start_resolved(store, store.config, spec, req, db=db)
    assert not result.ok
    assert "registration changed" in " ".join(result.evidence)
    launch.assert_not_called()
    marker.assert_not_called()
    assert not list(original_profile.glob("*.env"))


async def test_forget_during_real_start_waits_then_refuses(db, tmp_path):
    store = await registered_store(db, tmp_path)
    req = StartRequest(name="app")
    spec = await resolve_agent(store, req)
    entered, release = asyncio.Event(), asyncio.Event()
    running = False

    async def launch(*args, **kwargs):
        nonlocal running
        entered.set()
        await release.wait()
        running = True
        return StartResult(ok=True)

    async def exists(name):
        return running

    with (
        patch(f"{_OPS}.launch.start_agent", side_effect=launch),
        patch(f"{_OPS}.session_exists", side_effect=exists),
    ):
        start = asyncio.create_task(start_resolved(store, store.config, spec, req, db=db))
        await asyncio.wait_for(entered.wait(), 2)
        forget = asyncio.create_task(forget_agent(store, "app"))
        await asyncio.sleep(0)
        assert not forget.done()
        release.set()
        await asyncio.wait_for(start, 2)
        with pytest.raises(RuntimeError, match="running"):
            await asyncio.wait_for(forget, 2)
    assert store.agents.get("app") == spec


async def test_forget_removes_a_stopped_agent():
    store = AsyncMock()
    store.forget.return_value = True
    with patch(f"{_OPS}.session_exists", new_callable=AsyncMock, return_value=False):
        assert await forget_agent(store, "ike") is True
    store.forget.assert_awaited_once_with("ike")


async def test_forget_waits_for_a_start_in_progress_and_then_refuses():
    """The race the review named: a start launching between the check and the delete."""
    import asyncio

    from agent_backbone.services.agents.operations import lifecycle_lock

    store = AsyncMock()
    store.forget.return_value = True
    running = False

    async def _exists(name):
        return running

    with patch(f"{_OPS}.session_exists", side_effect=_exists):
        async with lifecycle_lock("ike"):  # a start holds the lock
            task = asyncio.create_task(forget_agent(store, "ike"))
            await asyncio.sleep(0)
            assert not task.done()  # forget is waiting for the start to finish
            running = True  # the start brought the session up
        with pytest.raises(RuntimeError):
            await task
    store.forget.assert_not_called()
