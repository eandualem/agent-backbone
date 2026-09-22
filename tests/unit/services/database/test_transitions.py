"""The transition record: created pending, stopped, finished; survives a reopen."""

from __future__ import annotations

from sqlalchemy import text

from agent_backbone.services.database import BackboneDB


async def test_lifecycle_fields(db):
    row = await db.transitions.create(
        agent_name="ike", requested_by="ike", runtime="codex", model="x", message="note"
    )
    assert row["status"] == "pending" and row["resume"] is False and row["result"] == {}
    assert await db.transitions.open_agents() == ["ike"]

    await db.transitions.mark_stopped(row["id"], start_at="2030-01-01T00:00:00.000000Z")
    stopped = await db.transitions.get(row["id"])
    assert stopped["stopped_at"] and stopped["start_at"] == "2030-01-01T00:00:00.000000Z"

    await db.transitions.finish(row["id"], "completed", {"ready": "ready"})
    done = await db.transitions.get(row["id"])
    assert done["status"] == "completed" and done["completed_at"]
    assert done["result"] == {"ready": "ready"}
    assert await db.transitions.open_for("ike") is None
    assert await db.transitions.open_agents() == []
    assert await db.transitions.pending() == []


async def test_explicit_start_time_is_kept_when_stopped(db):
    row = await db.transitions.create(agent_name="ike", start_at="2030-06-01T00:00:00.000000Z")
    await db.transitions.mark_stopped(row["id"], start_at="2030-01-01T00:00:00.000000Z")
    assert (await db.transitions.get(row["id"]))["start_at"] == "2030-06-01T00:00:00.000000Z"


async def test_finish_does_not_reopen_or_overwrite_a_closed_row(db):
    row = await db.transitions.create(agent_name="ike")
    await db.transitions.finish(row["id"], "failed", {"reason": "first"})
    await db.transitions.finish(row["id"], "completed", {"ready": "ready"})
    done = await db.transitions.get(row["id"])
    assert done["status"] == "failed" and done["result"] == {"reason": "first"}


async def test_list_for_is_newest_first_and_bounded(db):
    for _ in range(3):
        await db.transitions.create(agent_name="ike")
    await db.transitions.create(agent_name="leo")
    rows = await db.transitions.list_for("ike", limit=2)
    assert [r["agent_name"] for r in rows] == ["ike", "ike"]
    assert rows[0]["id"] > rows[1]["id"]


async def test_pending_rows_survive_a_process_restart(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'backbone.db'}"
    first = BackboneDB(url)
    await first.start()
    row = await first.transitions.create(agent_name="ike", message="carry on")
    await first.transitions.mark_stopped(row["id"], start_at="2030-01-01T00:00:00.000000Z")
    await first.stop()

    second = BackboneDB(url)
    await second.start()
    try:
        pending = await second.transitions.pending()
        assert [r["id"] for r in pending] == [row["id"]]
        assert pending[0]["message"] == "carry on" and pending[0]["stopped_at"]
        async with second.engine.begin() as conn:
            count = await conn.execute(text("SELECT COUNT(*) FROM agent_transitions"))
            assert count.scalar_one() == 1
    finally:
        await second.stop()
