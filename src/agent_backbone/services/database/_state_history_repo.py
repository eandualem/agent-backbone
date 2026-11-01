"""Retained observations of reconciled agent state."""

from __future__ import annotations

import json
import re
from datetime import timedelta

from sqlalchemy import text

from agent_backbone.services.database._repo import (
    CAPTURE_GENERATION_SQL,
    Repo,
    diagnostic_capture_policy,
    diagnostic_mode,
)
from agent_backbone.services.database._time import cutoff_iso, format_iso, now_iso, parse_iso

# Deliberately limited treatment of known credential forms. Debug evidence is
# opt-in local detail, not a guarantee that arbitrary text is free of secrets.
_CREDENTIALS = (
    # Mask labeled values before a header scheme can consume their labels.
    re.compile(
        r"(?i)((?:[\"']?(?:api[-_ ]?key|(?:access[_-]|refresh[_-])?token|secret|password|passwd)"
        r"[\"']?\s*[:=]\s*))(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s,;}&]+)"
    ),
    re.compile(
        r"(?i)(\bauthorization[\"']?\s*[:=]?\s*[\"']?(?:bearer|basic|token)\s+)[^\s\"',;)]+"
    ),
    re.compile(r"(?i)(\bbearer\s+)[^\s\"',;)]+"),
    re.compile(r"(://)[^/\s@]+(?=@)"),
)

_TOKEN_VALUES = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|"
    r"github_pat_[A-Za-z0-9_]{12,}|xox[baprs]-[A-Za-z0-9-]{12,})\b"
)


def stored_evidence(evidence: list[str]) -> list[str]:
    """Bounded explanatory text with known credential values masked, not a scrubber."""
    result = []
    for line in evidence[:10]:
        if not isinstance(line, str):
            continue
        for pattern in _CREDENTIALS:
            line = pattern.sub(lambda match: match[1] + "[redacted]", line)
        result.append(_TOKEN_VALUES.sub("[redacted]", line)[:2000])
    return result


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
            if await diagnostic_mode(conn) == "off":
                return
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
                text(
                    "DELETE FROM interaction_capture_checkpoints WHERE at < :cutoff "
                    "AND id != COALESCE((SELECT MAX(id) FROM interaction_capture_checkpoints "
                    "WHERE reason='diagnostics_mode_changed'),-1)"
                ),
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
            mode, generation = await diagnostic_capture_policy(conn)
            if mode == "off":
                return
            recorder_id = f"{recorder_id}:{generation}"
            for name, state, reason in states:
                identity = registrations.get(name)
                if not identity:
                    continue
                await conn.execute(
                    text(
                        """
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
                """
                        + " AND "
                        + CAPTURE_GENERATION_SQL
                        + "=:generation"
                    ),
                    {
                        "identity": identity,
                        "name": name,
                        "state": state,
                        "reason": reason,
                        "at": at,
                        "recorder": recorder_id,
                        "generation": generation,
                        "evidence": json.dumps(
                            stored_evidence(evidence.get(name, [])) if mode == "debug" else []
                        ),
                        "cutoff": format_iso(parse_iso(at) - timedelta(seconds=60)),
                    },
                )
