"""Agents repository — the known agents and the repositories they watch."""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from agent_backbone.services.database._time import now_iso


def _row_to_agent(row, watches: list[str]) -> dict:
    data = dict(row._mapping)
    data["tags"] = json.loads(data.get("tags") or "[]")
    data["env"] = json.loads(data.get("env") or "{}")
    data["watches"] = watches
    return data


async def list_agents(conn: AsyncConnection) -> list[dict]:
    """All known agents with their watched repositories."""
    watches: dict[str, list[str]] = {}
    result = await conn.execute(
        text("SELECT agent_name, repo FROM agent_watches ORDER BY agent_name, repo")
    )
    for row in result.fetchall():
        watches.setdefault(row._mapping["agent_name"], []).append(row._mapping["repo"])

    result = await conn.execute(text("SELECT * FROM agents ORDER BY name"))
    return [_row_to_agent(row, watches.get(row._mapping["name"], [])) for row in result.fetchall()]


async def upsert_agent(
    conn: AsyncConnection,
    name: str,
    *,
    dir: str,
    runtime: str,
    model: str | None,
    repo: str,
    tags: list[str],
    env: dict[str, str],
    description: str,
) -> None:
    now = now_iso()
    await conn.execute(
        text(
            """INSERT INTO agents
               (name, dir, runtime, model, repo, tags, env, description, created_at, updated_at)
               VALUES (:name, :dir, :runtime, :model, :repo, :tags, :env, :description, :now, :now)
               ON CONFLICT(name) DO UPDATE SET
                 dir = excluded.dir,
                 runtime = excluded.runtime,
                 model = excluded.model,
                 repo = excluded.repo,
                 tags = excluded.tags,
                 env = excluded.env,
                 description = excluded.description,
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
            "now": now,
        },
    )


async def touch_agent_started(conn: AsyncConnection, name: str) -> None:
    await conn.execute(
        text("UPDATE agents SET last_started_at = :now WHERE name = :name"),
        {"now": now_iso(), "name": name},
    )


async def delete_agent(conn: AsyncConnection, name: str) -> bool:
    await conn.execute(text("DELETE FROM agent_watches WHERE agent_name = :name"), {"name": name})
    result = await conn.execute(text("DELETE FROM agents WHERE name = :name"), {"name": name})
    return (result.rowcount or 0) > 0


async def add_watch(conn: AsyncConnection, name: str, repo: str) -> None:
    await conn.execute(
        text(
            """INSERT INTO agent_watches (agent_name, repo, created_at)
               VALUES (:name, :repo, :now)
               ON CONFLICT(agent_name, repo) DO NOTHING"""
        ),
        {"name": name, "repo": repo, "now": now_iso()},
    )


async def remove_watch(conn: AsyncConnection, name: str, repo: str) -> bool:
    result = await conn.execute(
        text("DELETE FROM agent_watches WHERE agent_name = :name AND repo = :repo"),
        {"name": name, "repo": repo},
    )
    return (result.rowcount or 0) > 0
