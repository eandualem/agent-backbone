"""Settings — dotted keys with JSON values."""

from __future__ import annotations

import json

from sqlalchemy import text

from agent_backbone.services.database._repo import Repo
from agent_backbone.services.database._time import now_iso


class SettingRepo(Repo):
    async def all(self) -> dict[str, object]:
        """All stored settings, JSON-decoded."""
        async with self._tx() as conn:
            result = await conn.execute(text("SELECT key, value FROM settings"))
            out: dict[str, object] = {}
            for row in result.fetchall():
                try:
                    out[row._mapping["key"]] = json.loads(row._mapping["value"])
                except ValueError:
                    out[row._mapping["key"]] = row._mapping["value"]
            return out

    async def set(self, key: str, value: object) -> None:
        async with self._tx() as conn:
            await conn.execute(
                text(
                    """INSERT INTO settings (key, value, updated_at)
                       VALUES (:key, :value, :now)
                       ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                                      updated_at = excluded.updated_at"""
                ),
                {"key": key, "value": json.dumps(value), "now": now_iso()},
            )

    async def delete(self, key: str) -> bool:
        async with self._tx() as conn:
            result = await conn.execute(text("DELETE FROM settings WHERE key = :key"), {"key": key})
            return (result.rowcount or 0) > 0
