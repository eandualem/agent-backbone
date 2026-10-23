"""Retained observations of reconciled agent state."""

from __future__ import annotations

from sqlalchemy import text

from agent_backbone.services.database._repo import Repo
from agent_backbone.services.database._time import cutoff_iso, now_iso


class StateHistoryRepo(Repo):
    async def observe(self, states: list[tuple[str, str, str | None]]) -> None:
        """Append a baseline or changed (state, reason); the feed serializes observations.

        Use observation time, never the hook's potentially old timestamp.
        The last persisted row supplies deduplication across process restarts.
        """
        if not states:
            return
        at = now_iso()
        async with self._tx() as conn:
            for agent, state, reason in states:
                await conn.execute(
                    text(
                        """INSERT INTO agent_state_history (agent, state, reason, at)
                           SELECT :agent, :state, :reason, :at
                           WHERE NOT EXISTS (
                               SELECT 1 FROM agent_state_history
                               WHERE id = (SELECT MAX(id) FROM agent_state_history
                                           WHERE agent = :agent)
                                 AND state = :state
                                 AND (reason = :reason OR (reason IS NULL AND :reason IS NULL))
                           )"""
                    ),
                    {"agent": agent, "state": state, "reason": reason, "at": at},
                )

    async def query(
        self,
        *,
        since: str,
        agent: str | None = None,
        limit: int = 200,
        before_id: int | None = None,
    ) -> list[dict]:
        clauses = ["at >= :since"]
        params: dict = {"since": since, "limit": limit}
        if agent is not None:
            clauses.append("agent = :agent")
            params["agent"] = agent
        if before_id is not None:
            clauses.append("id < CAST(:before_id AS BIGINT)")
            params["before_id"] = before_id
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    "SELECT id, agent, state, reason, at FROM agent_state_history WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY id DESC LIMIT :limit"
                ),
                params,
            )
            return [dict(row._mapping) for row in result.fetchall()]

    async def prune(self) -> int:
        """Keep seven days plus each agent's last observation for durable deduplication."""
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    """DELETE FROM agent_state_history WHERE at < :cutoff
                       AND id NOT IN (SELECT MAX(id) FROM agent_state_history GROUP BY agent)"""
                ),
                {"cutoff": cutoff_iso(days=7)},
            )
            return result.rowcount
