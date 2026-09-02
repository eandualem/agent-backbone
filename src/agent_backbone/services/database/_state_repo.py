"""Agent state — the database mirror of the hook state files."""

from __future__ import annotations

from sqlalchemy import text

from agent_backbone.services.database._repo import Repo
from agent_backbone.services.database._time import now_iso


class StateRepo(Repo):
    async def get(self, session_name: str) -> dict | None:
        async with self._tx() as conn:
            result = await conn.execute(
                text("SELECT * FROM agent_states WHERE session_name = :session_name"),
                {"session_name": session_name},
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None

    async def set(
        self,
        session_name: str,
        state: str,
        current_issue: int | None = None,
        started_at: str | None = None,
        ts: str | None = None,
        plan_file: str | None = None,
        plan_title: str | None = None,
        reason: str | None = None,
        current_repo: str | None = None,
    ) -> None:
        """Upsert an agent's state; ``started_at``, ``ts`` and the plan fields keep
        their old value when None."""
        async with self._tx() as conn:
            await conn.execute(
                text(
                    """INSERT INTO agent_states
                       (session_name, state, reason, current_issue, current_repo,
                        started_at, updated_at, ts, plan_file, plan_title)
                       VALUES (:session_name, :state, :reason, :current_issue, :current_repo,
                               :started_at, :updated_at, :ts, :plan_file, :plan_title)
                       ON CONFLICT(session_name) DO UPDATE SET
                         state = excluded.state,
                         reason = excluded.reason,
                         current_issue = excluded.current_issue,
                         current_repo = excluded.current_repo,
                         started_at = COALESCE(excluded.started_at, agent_states.started_at),
                         updated_at = excluded.updated_at,
                         ts = COALESCE(excluded.ts, agent_states.ts),
                         plan_file = COALESCE(excluded.plan_file, agent_states.plan_file),
                         plan_title = COALESCE(excluded.plan_title, agent_states.plan_title)"""
                ),
                {
                    "session_name": session_name,
                    "state": state,
                    "reason": reason,
                    "current_issue": current_issue,
                    "current_repo": current_repo,
                    "started_at": started_at,
                    "updated_at": now_iso(),
                    "ts": ts,
                    "plan_file": plan_file,
                    "plan_title": plan_title,
                },
            )

    async def all(self) -> list[dict]:
        async with self._tx() as conn:
            result = await conn.execute(text("SELECT * FROM agent_states ORDER BY session_name"))
            return [dict(row._mapping) for row in result.fetchall()]
