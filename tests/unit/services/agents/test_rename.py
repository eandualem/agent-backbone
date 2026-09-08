"""Renaming retains identity-bound receipts and refuses occupied destinations."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from agent_backbone.config import AgentSpec
from agent_backbone.services.agents import AgentStore, read_state_file, write_state_file


@pytest.fixture
async def store(db, tmp_path):
    store = AgentStore(db, tmp_path)
    await store.start()
    await store.register(
        AgentSpec(name="api", dir=str(tmp_path), runtime="codex", tags=("backend",))
    )
    with patch("agent_backbone.services.terminal.session_exists", AsyncMock(return_value=False)):
        yield store


async def test_rename_preserves_resume_configuration_and_scoped_routing(db, store):
    await store.watch("api", "acme/app")
    await store.set_setting("escalation.target", "api")
    await store.set_setting("telegram.topic_routes", {"12": "api", "13": "other"})
    write_state_file(
        store.config.state_dir, "api", {"runtime": "codex", "session_id": "conversation", "ts": 0}
    )
    for repo in ("acme/app", "acme/other"):
        await db.acks.record(4, "api", repo=repo)
        await db.deliveries.record(
            issue_number=4, target_entity="api", session_name="api", repo=repo, outcome="delivered"
        )
        await db.queue.enqueue(
            session_name="api", target_entity="api", repo=repo, issue_number=5, message="work"
        )
    await db.outbox.plan(7, [{"session_name": "api", "target_entity": "api", "repo": "acme/app"}])
    spec = await store.rename("api", "backend")
    assert spec.name == "backend" and spec.tags == ("backend",)
    assert spec.watches == ("acme/app",)
    assert store.agents.get("api") is None
    assert read_state_file(store.config.state_dir, "backend").session_id == "conversation"
    assert not (store.config.state_dir / "api.json").exists()
    assert store.config.escalation.target == "backend"
    assert store.config.telegram.topic_routes == {12: "backend", 13: "other"}
    outbox = (await db.outbox.entries(7))[0]
    assert outbox["recipient"] == outbox["delivery"]["session_name"] == "backend"
    async with db.engine.connect() as conn:
        acks = (await conn.execute(text("SELECT repo, target_entity FROM acknowledgments"))).all()
        queue = (
            await conn.execute(text("SELECT session_name, target_entity FROM message_queue"))
        ).all()
    assert set(acks) == {("acme/app", "backend"), ("acme/other", "backend")}
    assert set(queue) == {("backend", "backend")}


async def test_existing_history_rolls_back_without_leaving_resume_file(db, store):
    write_state_file(store.config.state_dir, "api", {"session_id": "original"})
    await db.acks.record(3, "taken", repo="acme/app")
    with pytest.raises(ValueError, match="history"):
        await store.rename("api", "taken")
    assert store.agents.get("api") is not None
    assert not (store.config.state_dir / "taken.json").exists()
    assert read_state_file(store.config.state_dir, "api").session_id == "original"


async def test_live_session_refused_before_mutation(store):
    with (
        patch("agent_backbone.services.terminal.session_exists", AsyncMock(return_value=True)),
        pytest.raises(ValueError, match="stop"),
    ):
        await store.rename("api", "backend")
    assert store.agents.get("api")


@pytest.mark.parametrize("new_name", ["../escape", "backbone", "api"])
async def test_unsafe_or_reserved_names_refused(store, new_name):
    with pytest.raises(ValueError):
        await store.rename("api", new_name)
    assert store.agents.get("api")


async def test_active_delivery_prevents_rename(db, store):
    await db.deliveries.claim(
        issue_number=9, target_entity="api", session_name="api", repo="acme/app", source="test"
    )
    with pytest.raises(ValueError, match="active delivery"):
        await store.rename("api", "backend")
    assert store.agents.get("api")


async def test_tag_updates_merge_concurrently_and_reject_internal_tags(store):
    await asyncio.gather(store.tag("api", ["python"]), store.tag("api", ["quality"]))
    assert set(store.agents.get("api").tags) == {"backend", "python", "quality"}
    await store.tag("api", ["quality"], remove=True)
    assert set(store.agents.get("api").tags) == {"backend", "python"}
    for tag in ("swarm:audit", "role:worker", "bad\nname", ""):
        with pytest.raises(ValueError):
            await store.tag("api", [tag])


@pytest.mark.parametrize("column", ["initiator", "coordinator"])
async def test_rename_rejects_name_in_completed_swarm_history(db, store, column):
    fields = dict(
        repo="acme/app", issue_number=1, initiator="", coordinator="", branch="b", worktree_dir="/w"
    )
    fields[column] = "taken"
    await db.swarms.create("previous", **fields)
    await db.swarms.set_status("previous", "done")
    write_state_file(store.config.state_dir, "api", {"session_id": "original"})
    with pytest.raises(ValueError, match="history"):
        await store.rename("api", "taken")
    assert store.agents.get("api") is not None
    assert not (store.config.state_dir / "taken.json").exists()
    assert (await db.swarms.get("previous"))[column] == "taken"
