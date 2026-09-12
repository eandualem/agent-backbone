"""Agents — the known agents and the repositories they watch."""

from __future__ import annotations

import json

from sqlalchemy import text

from agent_backbone.services.database._repo import Repo
from agent_backbone.services.database._time import now_iso


def _row_to_agent(row, watches: list[str]) -> dict:
    data = dict(row._mapping)
    data["tags"] = json.loads(data.get("tags") or "[]")
    data["env"] = json.loads(data.get("env") or "{}")
    data["watches"] = watches
    data["always_on"] = bool(data.get("always_on"))
    data["unattended"] = bool(data.get("unattended"))
    return data


class AgentRepo(Repo):
    async def list(self) -> list[dict]:
        """All known agents with their watched repositories."""
        async with self._tx() as conn:
            watches: dict[str, list[str]] = {}
            result = await conn.execute(
                text("SELECT agent_name, repo FROM agent_watches ORDER BY agent_name, repo")
            )
            for row in result.fetchall():
                watches.setdefault(row._mapping["agent_name"], []).append(row._mapping["repo"])

            result = await conn.execute(text("SELECT * FROM agents ORDER BY name"))
            return [
                _row_to_agent(row, watches.get(row._mapping["name"], []))
                for row in result.fetchall()
            ]

    async def upsert(
        self,
        name: str,
        *,
        dir: str,
        runtime: str,
        model: str | None,
        repo: str,
        tags: list[str],
        env: dict[str, str],
        description: str,
        always_on: bool = False,
        unattended: bool,
    ) -> None:
        """Replace an agent's fields; the caller must choose unattended explicitly."""
        async with self._tx() as conn:
            now = now_iso()
            await conn.execute(
                text(
                    """INSERT INTO agents
                       (name, dir, runtime, model, repo, tags, env, description,
                        always_on, unattended, created_at, updated_at)
                       VALUES (:name, :dir, :runtime, :model, :repo, :tags, :env,
                               :description, :always_on, :unattended, :now, :now)
                       ON CONFLICT(name) DO UPDATE SET
                         dir = excluded.dir,
                         runtime = excluded.runtime,
                         model = excluded.model,
                         repo = excluded.repo,
                         tags = excluded.tags,
                         env = excluded.env,
                         description = excluded.description,
                         always_on = excluded.always_on,
                         unattended = excluded.unattended,
                         updated_at = excluded.updated_at"""
                ),
                {
                    "name": name,
                    "dir": dir,
                    "runtime": runtime,
                    "model": model,
                    "repo": repo,
                    "tags": json.dumps(list(tags)),
                    "env": json.dumps(dict(env)),
                    "description": description,
                    "always_on": 1 if always_on else 0,
                    "unattended": 1 if unattended else 0,
                    "now": now,
                },
            )

    async def update_fields(self, name: str, changes: dict) -> bool:
        """Update only supplied fields, without recreating a forgotten agent."""
        allowed = {
            "dir",
            "runtime",
            "model",
            "repo",
            "tags",
            "env",
            "description",
            "always_on",
            "unattended",
        }
        if changes.keys() - allowed:
            raise ValueError("unknown agent fields")
        values = dict(changes)
        for key in ("tags", "env"):
            if key in values:
                values[key] = json.dumps(values[key])
        for key in ("always_on", "unattended"):
            if key in values:
                values[key] = int(values[key])
        assignments = [f"{key} = :{key}" for key in changes]
        if "runtime" in changes and "unattended" not in changes:
            assignments.append(
                "unattended = CASE WHEN runtime != :runtime THEN 0 ELSE unattended END"
            )
        assignments.append("updated_at = :now")
        async with self._tx() as conn:
            result = await conn.execute(
                text(f"UPDATE agents SET {', '.join(assignments)} WHERE name = :name"),
                {**values, "name": name, "now": now_iso()},
            )
            return bool(result.rowcount)

    async def touch_started(self, name: str) -> None:
        async with self._tx() as conn:
            await conn.execute(
                text("UPDATE agents SET last_started_at = :now WHERE name = :name"),
                {"now": now_iso(), "name": name},
            )

    async def delete(self, name: str) -> bool:
        async with self._tx() as conn:
            await conn.execute(
                text("DELETE FROM agent_watches WHERE agent_name = :name"), {"name": name}
            )
            result = await conn.execute(
                text("DELETE FROM agents WHERE name = :name"), {"name": name}
            )
            return (result.rowcount or 0) > 0

    async def rename(self, name: str, new_name: str) -> None:
        """Rekey identity and routing receipts in one transaction; never overwrite history."""
        references = (
            ("agents", "name"),
            ("agent_watches", "agent_name"),
            ("agent_states", "session_name"),
            ("usage_sessions", "agent_name"),
            ("acknowledgments", "target_entity"),
            ("deliveries", "session_name"),
            ("deliveries", "target_entity"),
            ("message_queue", "session_name"),
            ("message_queue", "target_entity"),
            ("event_outbox", "recipient"),
        )
        values = {"old": name, "new": new_name}
        async with self._tx() as conn:
            exists = await conn.execute(text("SELECT 1 FROM agents WHERE name = :old"), values)
            if not exists.first():
                raise KeyError(name)
            for table, column in (*references, ("swarms", "initiator"), ("swarms", "coordinator")):
                found = await conn.execute(
                    text(f"SELECT 1 FROM {table} WHERE {column} = :new LIMIT 1"), values
                )
                if found.first():
                    raise ValueError(
                        f"'{new_name}' already has configuration or history; choose another name"
                    )
            for sql in (
                "SELECT 1 FROM message_queue WHERE session_name = :old "
                "AND status = 'in_progress' LIMIT 1",
                "SELECT 1 FROM deliveries WHERE session_name = :old "
                "AND outcome = 'attempting' LIMIT 1",
                "SELECT 1 FROM swarms WHERE status = 'active' "
                "AND (initiator = :old OR coordinator = :old) LIMIT 1",
            ):
                if (await conn.execute(text(sql), values)).first():
                    raise ValueError(
                        "agent has an active delivery or swarm; retry after it finishes"
                    )
            rows = (
                await conn.execute(
                    text("SELECT event_id, delivery FROM event_outbox WHERE recipient = :old"),
                    values,
                )
            ).fetchall()
            for row in rows:
                delivery = json.loads(row.delivery)
                for key in ("session_name", "target_entity"):
                    if delivery.get(key) == name:
                        delivery[key] = new_name
                await conn.execute(
                    text(
                        "UPDATE event_outbox SET delivery = :delivery "
                        "WHERE event_id = :event AND recipient = :old"
                    ),
                    {**values, "event": row.event_id, "delivery": json.dumps(delivery)},
                )
            for table, column in references:
                await conn.execute(
                    text(f"UPDATE {table} SET {column} = :new WHERE {column} = :old"), values
                )
            for column in ("initiator", "coordinator"):
                await conn.execute(
                    text(f"UPDATE swarms SET {column} = :new WHERE {column} = :old"), values
                )
            settings = (
                await conn.execute(
                    text(
                        "SELECT key, value FROM settings "
                        "WHERE key IN ('escalation.target', 'telegram.topic_routes')"
                    )
                )
            ).fetchall()
            for row in settings:
                value = json.loads(row.value)
                if row.key == "escalation.target":
                    value = new_name if value == name else value
                else:
                    value = {k: new_name if v == name else v for k, v in value.items()}
                await conn.execute(
                    text("UPDATE settings SET value = :value, updated_at = :now WHERE key = :key"),
                    {"value": json.dumps(value), "now": now_iso(), "key": row.key},
                )

    async def add_watch(self, name: str, repo: str) -> None:
        async with self._tx() as conn:
            await conn.execute(
                text(
                    """INSERT INTO agent_watches (agent_name, repo, created_at)
                       VALUES (:name, :repo, :now)
                       ON CONFLICT(agent_name, repo) DO NOTHING"""
                ),
                {"name": name, "repo": repo, "now": now_iso()},
            )

    async def remove_watch(self, name: str, repo: str) -> bool:
        async with self._tx() as conn:
            result = await conn.execute(
                text("DELETE FROM agent_watches WHERE agent_name = :name AND repo = :repo"),
                {"name": name, "repo": repo},
            )
            return (result.rowcount or 0) > 0
