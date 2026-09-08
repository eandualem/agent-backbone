"""Durable replay boundaries survive database restart and event pruning."""

from agent_backbone.services.database import BackboneDB


async def test_cursor_is_repo_scoped_and_independent_of_event_retention(db):
    assert await db.events.poll_cursor("acme/a") is None
    await db.events.save_poll_cursor("acme/a", "2026-01-01T00:00:00Z")
    await db.events.save_poll_cursor("acme/b", "2026-02-01T00:00:00Z")
    await db.events.save_poll_cursor("acme/a", "2026-03-01T00:00:00Z")
    await db.events.prune(0)
    assert await db.events.poll_cursor("acme/a") == "2026-03-01T00:00:00Z"
    assert await db.events.poll_cursor("acme/b") == "2026-02-01T00:00:00Z"


async def test_cursor_survives_persistent_database_restart(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'cursor.db'}"
    async with BackboneDB.connect(url) as db:
        await db.events.save_poll_cursor("acme/a", "2026-03-01T00:00:00Z")
    async with BackboneDB.connect(url) as restarted:
        assert await restarted.events.poll_cursor("acme/a") == "2026-03-01T00:00:00Z"
