"""Tests for the agent store's record changes — the ``unattended`` flag in particular."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.config import AgentSpec
from agent_backbone.services.agents import AgentStore

_DETECT_REPO = "agent_backbone.services.agents.store.detect_repo"


async def _store(db, tmp_path) -> AgentStore:
    store = AgentStore(db, tmp_path)
    await store.start()
    project = tmp_path / "app"
    project.mkdir()
    await store.register(AgentSpec(name="app", dir=str(project), runtime="codex", unattended=True))
    return store


class TestUnattendedFollowsTheRuntime:
    """A freedom granted for one CLI's sandbox does not follow the agent to another."""

    async def test_changing_the_runtime_clears_the_flag(self, db, tmp_path):
        store = await _store(db, tmp_path)
        spec = await store.update("app", runtime="claude")
        assert spec.runtime == "claude" and spec.unattended is False

    async def test_unless_the_same_call_grants_it_again(self, db, tmp_path):
        store = await _store(db, tmp_path)
        spec = await store.update("app", runtime="claude", unattended=True)
        assert spec.unattended is True

    async def test_other_changes_keep_it(self, db, tmp_path):
        store = await _store(db, tmp_path)
        spec = await store.update("app", model="gpt-6-astra:high", runtime="codex")
        assert spec.unattended is True

    async def test_rediscovery_with_another_runtime_starts_attended(self, db, tmp_path):
        # `agent start app --runtime claude` goes through discover().
        store = await _store(db, tmp_path)
        with patch(_DETECT_REPO, new_callable=AsyncMock, return_value=""):
            same = await store.discover(tmp_path / "app")
            other = await store.discover(tmp_path / "app", runtime="claude")
        assert same.unattended is True
        assert other.unattended is False


class TestFlagsFromText:
    """The CLI's direct path hands over raw text; ``bool("False")`` must not mean on."""

    @pytest.mark.parametrize("raw", ["False", "false", "no", "off", "0", False, 0])
    async def test_every_spelling_of_off_turns_it_off(self, db, tmp_path, raw):
        store = await _store(db, tmp_path)
        assert (await store.update("app", unattended=raw)).unattended is False

    @pytest.mark.parametrize("raw", ["True", "yes", "1", True])
    async def test_every_spelling_of_on_turns_it_on(self, db, tmp_path, raw):
        store = await _store(db, tmp_path)
        await store.update("app", unattended=False)
        assert (await store.update("app", unattended=raw)).unattended is True

    async def test_anything_else_is_refused(self, db, tmp_path):
        store = await _store(db, tmp_path)
        with pytest.raises(ValueError, match="unattended must be true or false"):
            await store.update("app", unattended="maybe")
        with pytest.raises(ValueError, match="always_on must be true or false"):
            await store.update("app", always_on="sometimes")


async def test_concurrent_updates_preserve_fields_even_from_stale_stores(db, tmp_path):
    first = await _store(db, tmp_path)
    second = AgentStore(db, tmp_path)
    await second.start()
    await asyncio.gather(
        first.update(name="app", model="new-model"),
        second.update(name="app", description="new-description"),
    )
    await first.refresh()
    spec = first.agents.get("app")
    assert (spec.model, spec.description, spec.unattended) == ("new-model", "new-description", True)


async def test_update_from_stale_snapshot_never_recreates_a_forgotten_agent(db, tmp_path):
    first = await _store(db, tmp_path)
    second = AgentStore(db, tmp_path)
    await second.start()
    await first.forget("app")
    with pytest.raises(KeyError, match="app"):
        await second.update("app", description="late update")
    assert "app" not in {row["name"] for row in await db.agents.list()}


async def test_concurrent_discovery_reserves_distinct_names(db, tmp_path):
    store = AgentStore(db, tmp_path)
    await store.start()
    paths = [tmp_path / parent / "project" for parent in ("one", "two")]
    for path in paths:
        path.mkdir(parents=True)
    with patch(_DETECT_REPO, AsyncMock(return_value="")):
        specs = await asyncio.gather(*(store.register_directory(path) for path in paths))
    assert {spec.name for spec in specs} == {"project", "project-2"}
    assert {spec.path for spec in specs} == set(paths)


async def test_rediscovery_preserves_update_while_waiting_for_agent_lock(db, tmp_path):
    from agent_backbone.services.agents import lifecycle_lock

    store = await _store(db, tmp_path)
    with patch(_DETECT_REPO, AsyncMock(return_value="")):
        async with lifecycle_lock("app"):
            rediscover = asyncio.create_task(store.register_directory(tmp_path / "app"))
            await asyncio.sleep(0)
            await store.update("app", description="keep this", model="keep-model")
        spec = await asyncio.wait_for(rediscover, 2)
    assert (spec.description, spec.model) == ("keep this", "keep-model")
