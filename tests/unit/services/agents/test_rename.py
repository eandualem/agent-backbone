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


async def test_rename_moves_pending_hook_context_offers(db, store):
    from agent_backbone.hooks.backbone_state import claim_context, offer_context

    assert offer_context(store.config.state_dir, "api", "7", "[via:gmail] mail")
    await store.rename("api", "desk")
    assert claim_context(store.config.state_dir, "api", "7") == "missing"
    assert claim_context(store.config.state_dir, "desk", "7") == "claimed"


async def test_forget_clears_pending_hook_context_offers(db, store):
    from agent_backbone.hooks.backbone_state import claim_context, offer_context

    assert offer_context(store.config.state_dir, "api", "7", "[via:gmail] mail")
    assert await store.forget("api")
    assert claim_context(store.config.state_dir, "api", "7") == "missing"


async def test_rename_moves_the_skill_manifest(db, store):
    """Left under the old name, it would keep links alive for an agent that is gone."""
    from agent_backbone.skills import manifest_path

    old = manifest_path(store.config.data_dir, "api")
    old.parent.mkdir(parents=True)
    old.write_text('{"repo": "/r", "links": []}')
    await store.rename("api", "desk")
    assert not old.exists()
    assert manifest_path(store.config.data_dir, "desk").exists()


async def test_forget_releases_the_agents_own_skill_links(db, store, tmp_path):
    from agent_backbone.skills import manifest_path

    skills = tmp_path / "skill-store"
    (skills / "tidy").mkdir(parents=True)
    await db.settings.set("skills.store", str(skills))
    await store.refresh()
    link = tmp_path / ".agents" / "skills" / "tidy"
    link.parent.mkdir(parents=True)
    link.symlink_to(skills / "tidy")
    manifest = manifest_path(store.config.data_dir, "api")
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        f'{{"repo": "{tmp_path}", "links": [".agents/skills/tidy"]}}', encoding="utf-8"
    )
    assert await store.forget("api")
    assert not link.is_symlink()
    assert not manifest.exists()


async def test_forget_releases_links_in_the_checkout_the_manifest_records(db, store, tmp_path):
    """The agent's directory changed after its last launch: its links are in the old one."""
    from agent_backbone.skills import manifest_path

    skills = tmp_path / "skill-store"
    (skills / "tidy").mkdir(parents=True)
    await db.settings.set("skills.store", str(skills))
    old_checkout = tmp_path / "old-checkout"
    old_link = old_checkout / ".agents" / "skills" / "tidy"
    old_link.parent.mkdir(parents=True)
    old_link.symlink_to(skills / "tidy")
    same_name = tmp_path / ".agents" / "skills" / "tidy"  # in the agent's current directory
    same_name.parent.mkdir(parents=True)
    same_name.symlink_to(skills / "tidy")
    await store.refresh()
    manifest = manifest_path(store.config.data_dir, "api")
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        f'{{"repo": "{old_checkout}", "links": [".agents/skills/tidy"]}}', encoding="utf-8"
    )
    assert await store.forget("api")
    assert not old_link.is_symlink()
    assert same_name.is_symlink()


async def test_forget_keeps_the_manifest_when_its_links_cannot_be_released(db, store, tmp_path):
    from agent_backbone.skills import manifest_path

    await db.settings.set("skills.store", str(tmp_path / "skill-store"))
    await store.refresh()
    manifest = manifest_path(store.config.data_dir, "api")
    manifest.parent.mkdir(parents=True)
    manifest.write_text(f'{{"repo": "{tmp_path}", "links": [".agents/skills/tidy"]}}')
    with patch("agent_backbone.skills.materialize", side_effect=OSError("read-only checkout")):
        assert await store.forget("api")
    assert manifest.exists()  # a later cleanup still knows those links


async def test_a_manifest_that_cannot_move_aborts_the_rename(db, store):
    from agent_backbone.skills import manifest_path

    old = manifest_path(store.config.data_dir, "api")
    old.parent.mkdir(parents=True)
    old.write_text('{"repo": "/r", "links": []}')
    with patch("pathlib.Path.rename", side_effect=OSError("busy")), pytest.raises(OSError):
        await store.rename("api", "desk")
    assert "api" in {row["name"] for row in await db.agents.list()} and old.exists()


async def test_a_failed_rename_moves_the_manifest_back(db, store):
    from agent_backbone.skills import manifest_path

    old = manifest_path(store.config.data_dir, "api")
    old.parent.mkdir(parents=True)
    old.write_text('{"repo": "/r", "links": []}')
    with patch.object(db.agents, "rename", AsyncMock(side_effect=RuntimeError("db down"))):
        with pytest.raises(RuntimeError):
            await store.rename("api", "desk")
    assert old.exists() and not manifest_path(store.config.data_dir, "desk").exists()


async def test_rename_keeps_a_pending_restart(db, store):
    row = await db.transitions.create(agent_name="api", delay_seconds=3600, message="go on")
    await store.rename("api", "backend")
    moved = await db.transitions.get(row["id"])
    assert moved["agent_name"] == "backend" and moved["status"] == "pending"
