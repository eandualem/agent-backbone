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
        unattended: bool = False,
    ) -> None:
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
