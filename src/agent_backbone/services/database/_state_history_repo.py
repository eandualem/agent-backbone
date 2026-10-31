"""Retained observations of reconciled agent state."""

from __future__ import annotations

import json
import re
from datetime import timedelta

from sqlalchemy import text

from agent_backbone.services.database._repo import Repo
from agent_backbone.services.database._time import cutoff_iso, format_iso, now_iso, parse_iso

# Provider banners are terminal text and can quote a credential. Stored
# evidence keeps the words that explain the state, never the secret.
_SECRETS = (
    re.compile(r"(?i)(bearer\s+)[^\s\"',;]+"),
    re.compile(
        r"(?i)((?:authorization|api[-_ ]?key|token|secret|password|passwd)\s*[:=]\s*)"
        r"[^\s\"',;&]+"
    ),
    re.compile(r"(?i)([?&][^=&\s]*(?:key|token|secret|sig|password|auth)[^=&\s]*=)[^&\s]+"),
    re.compile(r"(://[^/\s:@]+:)[^@\s]+(@)"),
    re.compile(r"()\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abpr]-[\w-]{10,})"),
)


def _redacted(evidence: list[str]) -> list[str]:
    out = []
    for line in evidence:
        for pattern in _SECRETS:
            line = pattern.sub(
                lambda m: m.group(1) + "[redacted]" + (m.group(2) if m.lastindex == 2 else ""),
                line,
            )
        out.append(line)
    return out


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
        cutoff = cutoff_iso(days=7)
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    """DELETE FROM agent_state_history WHERE at < :cutoff
                       AND id NOT IN (SELECT MAX(id) FROM agent_state_history GROUP BY agent)"""
                ),
                {"cutoff": cutoff},
            )
            count = result.rowcount
            await conn.execute(
                text("DELETE FROM registered_state_observations WHERE at < :cutoff"),
                {"cutoff": cutoff},
            )
            await conn.execute(
                text("DELETE FROM interaction_capture_checkpoints WHERE at < :cutoff"),
                {"cutoff": cutoff},
            )
            return count

    async def observe_registered(self, states, *, registrations, evidence, recorder_id):
        """Record prospective stable identities; never resolve old observations by name.

        Unchanged samples are limited to one per minute. Changes and recorder
        boundaries retain their first sample. Missing observations remain gaps.
        """
        at = now_iso()
        async with self._tx() as conn:
            for name, state, reason in states:
                identity = registrations.get(name)
                if not identity:
                    continue
                await conn.execute(
                    text("""
                    INSERT INTO registered_state_observations
                    (agent_id,name,state,reason,at,recorder_id,evidence)
                    SELECT :identity,:name,:state,:reason,:at,:recorder,:evidence
                    WHERE EXISTS (SELECT 1 FROM agents
                                      WHERE name=:name AND report_identity=:identity)
                    AND NOT EXISTS (
                        SELECT 1 FROM registered_state_observations
                        WHERE id=(SELECT MAX(id) FROM registered_state_observations
                                  WHERE agent_id=:identity)
                        AND recorder_id=:recorder AND state=:state
                        AND (reason=:reason OR (reason IS NULL AND :reason IS NULL))
                        AND at > :cutoff
                    )
                """),
                    {
                        "identity": identity,
                        "name": name,
                        "state": state,
                        "reason": reason,
                        "at": at,
                        "recorder": recorder_id,
                        "evidence": json.dumps(_redacted(evidence.get(name, []))),
                        "cutoff": format_iso(parse_iso(at) - timedelta(seconds=60)),
                    },
                )
