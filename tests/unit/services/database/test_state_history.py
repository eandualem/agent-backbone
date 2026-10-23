"""State history survives restarts and pruning without manufacturing transitions."""

from unittest.mock import patch

from agent_backbone.services.database import BackboneDB

_CLOCK = "agent_backbone.services.database._state_history_repo.now_iso"


async def test_retention_and_restart_deduplication(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'history.db'}"
    async with BackboneDB.connect(url) as db:
        with patch(_CLOCK, return_value="2026-10-01T00:00:00.000000Z"):
            await db.state_history.observe([("worker", "busy", None), ("quiet", "offline", None)])
        with patch(_CLOCK, return_value="2026-10-02T00:00:00.000000Z"):
            await db.state_history.observe([("worker", "idle", None)])
        with patch(_CLOCK, return_value="2026-10-16T00:00:00.000000Z"):
            await db.state_history.observe([("worker", "busy", None)])
        with patch(
            "agent_backbone.services.database._state_history_repo.cutoff_iso",
            return_value="2026-10-16T00:00:00.000000Z",
        ) as cutoff:
            assert await db.state_history.prune() == 2
            cutoff.assert_called_once_with(days=7)
        before = await db.state_history.query(since="2000-01-01T00:00:00.000000Z")
        assert [r["agent"] for r in before] == ["worker", "quiet"]
    async with BackboneDB.connect(url) as db:
        await db.state_history.observe([("worker", "busy", None), ("quiet", "offline", None)])
        assert await db.state_history.query(since="2000-01-01T00:00:00.000000Z") == before
        await db.state_history.observe([("worker", "idle", None)])
        latest = await db.state_history.query(since="2000-01-01T00:00:00.000000Z", limit=1)
        assert latest[0]["id"] > before[0]["id"]
