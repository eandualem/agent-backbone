"""Agent transitions: perform the stop and the later start an agent asked for.

Runs every few seconds. A pending transition whose session has not been
stopped yet is stopped now (``tmux kill-session``, idempotent: a session
that already vanished is simply gone) and its start time recorded. When
that time comes, the replacement starts through the same operation as
``agent start`` — the record update, the brief, the hooks and the readiness
wait — and the continuation message, if any, is handed to it. Every row
ends ``completed`` or ``failed`` with what happened; nothing is retried or
substituted.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agent_backbone.services.agents import launch, lifecycle_lock
from agent_backbone.services.agents.operations import StartRequest, resolve_agent, start_resolved
from agent_backbone.services.agents.transitions import SOURCE, due_after
from agent_backbone.services.database import now_iso
from agent_backbone.services.jobs.diagnostics import observe_job
from agent_backbone.services.routing import safe_deliver

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.agents import AgentStore
    from agent_backbone.services.database import BackboneDB

log = logging.getLogger(__name__)

JOB = "agent-transitions"
INTERVAL_SECONDS = 5


async def run_transitions(
    config: Callable[[], BackboneConfig], store: AgentStore, db: BackboneDB
) -> dict[str, str]:
    """One tick: advance every pending transition. Returns agent -> what happened."""
    summary: dict[str, str] = {}
    try:
        rows = await db.transitions.pending()
    except Exception as exc:
        log.exception("Could not read pending transitions")
        await observe_job(db, source=JOB, stage="read", error_type=type(exc).__name__)
        return summary
    for row in rows:
        name = row["agent_name"]
        try:
            summary[name] = await _advance(config(), store, db, row)
        except Exception as exc:
            log.exception("Transition #%s for '%s' failed", row["id"], name)
            await db.transitions.finish(
                row["id"], "failed", {"reason": f"unexpected error: {type(exc).__name__}"}
            )
            await observe_job(
                db, source=JOB, stage="advance", agent_name=name, error_type=type(exc).__name__
            )
            summary[name] = "failed"
    return summary


async def _advance(config: BackboneConfig, store: AgentStore, db: BackboneDB, row: dict) -> str:
    name = row["agent_name"]
    if config.agents.get(name) is None:
        await db.transitions.finish(row["id"], "failed", {"reason": "agent is no longer known"})
        return "failed"
    if row["stopped_at"] is None:
        async with lifecycle_lock(name):
            stopped = await launch.stop_agent(name)
        if not stopped:
            await db.transitions.finish(
                row["id"], "failed", {"reason": "could not stop the session"}
            )
            return "failed"
        start_at = row["start_at"] or due_after(datetime.now(UTC), row["delay_seconds"])
        await db.transitions.mark_stopped(row["id"], start_at=start_at)
        if not row["start"]:
            await db.transitions.finish(row["id"], "completed", {"stopped": True})
            return "stopped"
        if start_at > now_iso():
            return "stopped"
        return await _start(config, store, db, {**row, "start_at": start_at})
    if row["start_at"] > now_iso():
        return "waiting"
    return await _start(config, store, db, row)


async def _start(config: BackboneConfig, store: AgentStore, db: BackboneDB, row: dict) -> str:
    name = row["agent_name"]
    req = StartRequest(name=name, runtime=row["runtime"], model=row["model"], resume=row["resume"])
    try:
        spec = await resolve_agent(store, req)
        result = await start_resolved(store, config, spec, req, db=db)
    except (KeyError, ValueError) as exc:
        await db.transitions.finish(row["id"], "failed", {"reason": str(exc)})
        return "failed"
    outcome = {
        "runtime": req.runtime or spec.runtime,
        "model": req.model if req.model is not None else spec.model,
        "resume": row["resume"],
        "ready": result.ready,
        "evidence": list(result.evidence),
    }
    if result.already_running:
        outcome["reason"] = "the session was already running at start time; not started here"
        await db.transitions.finish(row["id"], "failed", outcome)
        return "failed"
    if not result.ok or result.ready == "exited":
        outcome["reason"] = "the replacement did not start"
        await db.transitions.finish(row["id"], "failed", outcome)
        return "failed"
    if row["message"]:
        sender = row["requested_by"] or "backbone"
        report = await safe_deliver(
            name,
            f"[via:backbone from:{sender}] {row['message']}",
            config,
            db=db,
            delivery_kind="direct_message",
            source=SOURCE,
            sender=sender,
        )
        outcome["message"] = report.outcome.value
        if report.queue is not None:
            outcome["message_queue"] = report.queue
    await db.transitions.finish(row["id"], "completed", outcome)
    return "started"
