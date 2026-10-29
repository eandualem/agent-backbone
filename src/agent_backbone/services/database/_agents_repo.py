"""Agents — the known agents and the repositories they watch."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text

from agent_backbone.services.database._repo import Repo
from agent_backbone.services.database._time import now_iso


def _row_to_agent(row, watches: list[str], subscriptions: list[dict]) -> dict:
    data = dict(row._mapping)
    data["tags"] = json.loads(data.get("tags") or "[]")
    data["env"] = json.loads(data.get("env") or "{}")
    data["watches"] = watches
    data["subscriptions"] = subscriptions
    data["always_on"] = bool(data.get("always_on"))
    data["unattended"] = bool(data.get("unattended"))
    data["inbox_only"] = bool(data.get("inbox_only"))
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

            subscriptions: dict[str, list[dict]] = {}
            result = await conn.execute(
                text(
                    "SELECT id, agent_name, source, query, priority FROM agent_subscriptions "
                    "ORDER BY agent_name, id"
                )
            )
            for row in result.mappings():
                subscriptions.setdefault(row["agent_name"], []).append(
                    {
                        "id": row["id"],
                        "source": row["source"],
                        "filter": row["query"],
                        "priority": row["priority"],
                    }
                )

            result = await conn.execute(text("SELECT * FROM agents ORDER BY name"))
            return [
                _row_to_agent(
                    row,
                    watches.get(row._mapping["name"], []),
                    subscriptions.get(row._mapping["name"], []),
                )
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
        inbox_only: bool = False,
    ) -> None:
        """Replace an agent's fields; the caller must choose unattended explicitly."""
        async with self._tx() as conn:
            now = now_iso()
            await conn.execute(
                text(
                    """INSERT INTO agents
                       (name, dir, runtime, model, repo, tags, env, description,
                        always_on, unattended, inbox_only, created_at, updated_at, report_identity)
                       VALUES (:name, :dir, :runtime, :model, :repo, :tags, :env,
                               :description, :always_on, :unattended, :inbox_only,
                               :now, :now, :identity)
                       ON CONFLICT(name) DO UPDATE SET
                         report_identity = COALESCE(agents.report_identity,
                                                    excluded.report_identity),
                         dir = excluded.dir,
                         runtime = excluded.runtime,
                         model = excluded.model,
                         repo = excluded.repo,
                         tags = excluded.tags,
                         env = excluded.env,
                         description = excluded.description,
                         always_on = excluded.always_on,
                         unattended = excluded.unattended,
                         inbox_only = excluded.inbox_only,
                         updated_at = excluded.updated_at"""
                ),
                {
                    "name": name,
                    "identity": uuid.uuid4().hex,
                    "dir": dir,
                    "runtime": runtime,
                    "model": model,
                    "repo": repo,
                    "tags": json.dumps(list(tags)),
                    "env": json.dumps(dict(env)),
                    "description": description,
                    "always_on": 1 if always_on else 0,
                    "unattended": 1 if unattended else 0,
                    "inbox_only": 1 if inbox_only else 0,
                    "now": now,
                },
            )

    async def update_fields(
        self, name: str, changes: dict, *, expected_registration_id: str | None = None
    ) -> bool:
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
            "inbox_only",
        }
        if changes.keys() - allowed:
            raise ValueError("unknown agent fields")
        values = dict(changes)
        for key in ("tags", "env"):
            if key in values:
                values[key] = json.dumps(values[key])
        for key in ("always_on", "unattended", "inbox_only"):
            if key in values:
                values[key] = int(values[key])
        assignments = [f"{key} = :{key}" for key in changes]
        if "runtime" in changes and "unattended" not in changes:
            assignments.append(
                "unattended = CASE WHEN runtime != :runtime THEN 0 ELSE unattended END"
            )
        assignments.append("updated_at = :now")
        guard = " AND report_identity = :identity" if expected_registration_id is not None else ""
        async with self._tx() as conn:
            result = await conn.execute(
                text(f"UPDATE agents SET {', '.join(assignments)} WHERE name = :name{guard}"),
                {**values, "name": name, "now": now_iso(), "identity": expected_registration_id},
            )
            return bool(result.rowcount)

    async def started_at(self, name: str) -> str | None:
        """When the agent's current conversation started fresh, if it ever did."""
        async with self._tx() as conn:
            return await conn.scalar(
                text("SELECT conversation_started_at FROM agents WHERE name = :name"),
                {"name": name},
            )

    async def touch_started(self, name: str) -> None:
        async with self._tx() as conn:
            await conn.execute(
                text("UPDATE agents SET last_started_at = :now WHERE name = :name"),
                {"now": now_iso(), "name": name},
            )

    async def conversation_started(self, name: str, at: str) -> None:
        """Record that a fresh conversation started at ``at`` (taken before the launch)."""
        async with self._tx() as conn:
            await conn.execute(
                text("UPDATE agents SET conversation_started_at = :at WHERE name = :name"),
                {"at": at, "name": name},
            )

    async def registration_name(self, identity: str) -> str | None:
        async with self._tx() as conn:
            return await conn.scalar(
                text("SELECT name FROM agents WHERE report_identity=:identity"),
                {"identity": identity},
            )

    async def registration_matches(self, name: str, identity: str | None) -> bool:
        if not identity:
            return False
        async with self._tx() as conn:
            return bool(
                await conn.scalar(
                    text("SELECT 1 FROM agents WHERE name=:name AND report_identity=:identity"),
                    {"name": name, "identity": identity},
                )
            )

    async def delete(self, name: str) -> bool:
        async with self._tx() as conn:
            if conn.dialect.name == "postgresql":
                # Serialize retirement with bound acknowledgment writes.
                await conn.execute(
                    text("SELECT 1 FROM agents WHERE name=:name FOR UPDATE"), {"name": name}
                )
            # Retain bodies and history, but never hand them to a reused name.
            await conn.execute(
                text(
                    "INSERT INTO deliveries (operation_id,kind,repo,issue_number,target_entity,"
                    "session_name,outcome,source,preview,created_at,registration_id) "
                    "SELECT operation_id,delivery_kind,repo,issue_number,"
                    "COALESCE(target_entity,session_name),session_name,'expired',"
                    "'registration-retired',substr(message,1,120),:now,registration_id "
                    "FROM message_queue WHERE session_name=:name "
                    "AND status IN ('pending','in_progress','checkpoint','uncertain')"
                ),
                {"name": name, "now": now_iso()},
            )
            await conn.execute(
                text(
                    "UPDATE message_queue SET status='expired', delivered_at=:now, leased_at=NULL "
                    "WHERE session_name=:name "
                    "AND status IN ('pending','in_progress','checkpoint','uncertain')"
                ),
                {"name": name, "now": now_iso()},
            )
            await conn.execute(
                text(
                    "UPDATE event_outbox SET status='skipped', updated_at=:now "
                    "WHERE recipient=:name AND status IN ('pending','failed')"
                ),
                {"name": name, "now": now_iso()},
            )
            await conn.execute(
                text(
                    "UPDATE deliveries SET superseded_at=:now WHERE session_name=:name "
                    "AND superseded_at IS NULL"
                ),
                {"name": name, "now": now_iso()},
            )
            await conn.execute(
                text("DELETE FROM agent_watches WHERE agent_name = :name"), {"name": name}
            )
            await conn.execute(
                text("DELETE FROM agent_subscriptions WHERE agent_name = :name"), {"name": name}
            )
            # A replacement registration must not inherit a prior acknowledgment.
            # Publication receipts and delivery history remain available.
            await conn.execute(
                text(
                    "UPDATE acknowledgments SET acknowledged_at='', cleared_at=:now, "
                    "cleared_comment_id=NULL WHERE target_entity=:name"
                ),
                {"name": name, "now": now_iso()},
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
            ("agent_subscriptions", "agent_name"),
            ("agent_states", "session_name"),
            ("usage_sessions", "agent_name"),
            ("acknowledgments", "target_entity"),
            ("deliveries", "session_name"),
            ("deliveries", "target_entity"),
            ("message_queue", "session_name"),
            ("message_queue", "target_entity"),
            ("event_outbox", "recipient"),
            ("agent_transitions", "agent_name"),
            # Where a confirmation went and who claimed it; the signed recipient stays.
            ("signing_receipts", "delivered_to"),
            ("signing_grants", "agent"),
        )
        values = {"old": name, "new": new_name}
        async with self._tx() as conn:
            lock = " FOR UPDATE" if conn.dialect.name == "postgresql" else ""
            exists = await conn.execute(
                text("SELECT 1 FROM agents WHERE name = :old" + lock), values
            )
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

    async def add_subscription(self, name: str, source: str, query: str, priority: str) -> int:
        """Record a subscription; the same filter again only changes its priority."""
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    """INSERT INTO agent_subscriptions
                       (agent_name, source, query, priority, created_at)
                       VALUES (:name, :source, :query, :priority, :now)
                       ON CONFLICT(agent_name, source, query)
                       DO UPDATE SET priority = excluded.priority
                       RETURNING id"""
                ),
                {
                    "name": name,
                    "source": source,
                    "query": query,
                    "priority": priority,
                    "now": now_iso(),
                },
            )
            return int(result.scalar_one())

    async def remove_subscription(self, name: str, subscription_id: int) -> bool:
        async with self._tx() as conn:
            result = await conn.execute(
                text("DELETE FROM agent_subscriptions WHERE agent_name = :name AND id = :id"),
                {"name": name, "id": subscription_id},
            )
            return (result.rowcount or 0) > 0
