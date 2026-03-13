"""Agent state repository."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from agent_backbone.services.database._repo_utils import row_to_dict, rows_to_dicts, utc_now_iso


async def get_agent_state(
    conn: AsyncConnection,
    session_name: str,
) -> dict | None:
    """Get the current state record for an agent session."""
    result = await conn.execute(
        text("SELECT * FROM agent_states WHERE session_name = :session_name"),
        {"session_name": session_name},
    )
    return row_to_dict(result.fetchone())


async def set_agent_state(
    conn: AsyncConnection,
    session_name: str,
    state: str,
    current_issue: int | None = None,
    last_activity: str | None = None,
    started_at: str | None = None,
    entity: str | None = None,
    context: str | None = None,
    ts: str | None = None,
    plan_file: str | None = None,
    plan_title: str | None = None,
) -> None:
    """Upsert agent state."""
    now = utc_now_iso()
    await conn.execute(
        text(
            """INSERT INTO agent_states
               (session_name, state, current_issue,
                last_activity, started_at, updated_at,
                entity, context, ts, plan_file, plan_title)
               VALUES (:session_name, :state, :current_issue,
                       :last_activity, :started_at, :updated_at,
                       :entity, :context, :ts, :plan_file, :plan_title)
               ON CONFLICT(session_name) DO UPDATE SET
                 state = excluded.state,
                 current_issue = excluded.current_issue,
                 last_activity = COALESCE(
                     excluded.last_activity,
                     agent_states.last_activity),
                 started_at = COALESCE(
                     excluded.started_at,
                     agent_states.started_at),
                 updated_at = excluded.updated_at,
                 entity = COALESCE(
                     excluded.entity,
                     agent_states.entity),
                 context = COALESCE(
                     excluded.context,
                     agent_states.context),
                 ts = COALESCE(
                     excluded.ts,
                     agent_states.ts),
                 plan_file = COALESCE(
                     excluded.plan_file,
                     agent_states.plan_file),
                 plan_title = COALESCE(
                     excluded.plan_title,
                     agent_states.plan_title)"""
        ),
        {
            "session_name": session_name,
            "state": state,
            "current_issue": current_issue,
            "last_activity": last_activity,
            "started_at": started_at,
            "updated_at": now,
            "entity": entity,
            "context": context,
            "ts": ts,
            "plan_file": plan_file,
            "plan_title": plan_title,
        },
    )


async def get_all_agent_states(
    conn: AsyncConnection,
) -> list[dict]:
    """Get state records for all tracked agents."""
    result = await conn.execute(text("SELECT * FROM agent_states ORDER BY session_name"))
    return rows_to_dicts(result.fetchall())
