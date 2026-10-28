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
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agent_backbone.services.agents import launch, lifecycle_lock
from agent_backbone.services.agents.credentials import profile_lock
from agent_backbone.services.agents.operations import StartRequest, resolve_agent, start_resolved
from agent_backbone.services.agents.transitions import SOURCE, due_after, validate_resume
from agent_backbone.services.database import now_iso
from agent_backbone.services.jobs.diagnostics import observe_job
from agent_backbone.services.routing import envelope, safe_deliver
from agent_backbone.services.terminal import query_environment_var

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
    if row.get("registration_id"):
        async with lifecycle_lock(name):
            if not await db.agents.registration_matches(name, row["registration_id"]):
                await db.transitions.finish(
                    row["id"], "failed", {"reason": "agent registration changed"}
                )
                return "failed"
            await store.refresh()
            return await _advance_current(replace(config, agents=store.agents), store, db, row)
    return await _advance_current(config, store, db, row)


async def _advance_current(
    config: BackboneConfig, store: AgentStore, db: BackboneDB, row: dict
) -> str:
    name = row["agent_name"]
    spec = config.agents.get(name)
    if spec is None:
        await db.transitions.finish(row["id"], "failed", {"reason": "agent is no longer known"})
        return "failed"
    if row.get("registration_id") and spec.registration_id != row["registration_id"]:
        await db.transitions.finish(row["id"], "failed", {"reason": "agent registration changed"})
        return "failed"
    if spec.inbox_only:
        # Made inbox-only after the request: a session with its name is not its own.
        await db.transitions.finish(row["id"], "failed", {"reason": "agent is inbox-only"})
        return "failed"
    if row["stopped_at"] is None:
        async with lifecycle_lock(name):
            if row["resume"]:
                try:
                    await store.refresh()
                    current = store.agents.get(name)
                    if current is None or current.inbox_only:
                        raise ValueError("agent is no longer available for a session restart")
                    validate_resume(config, current, row["runtime"] or current.runtime)
                except ValueError as exc:
                    await db.transitions.finish(row["id"], "failed", {"reason": str(exc)})
                    return "failed"
            # The direct CLI shares this guard with registration replacement and
            # launch; a process-local lifecycle lock alone cannot protect a name.
            async with profile_lock(config.data_dir):
                if row.get("registration_id") and not await db.agents.registration_matches(
                    name, row["registration_id"]
                ):
                    await db.transitions.finish(
                        row["id"], "failed", {"reason": "agent registration changed"}
                    )
                    return "failed"
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
    if not row["start"]:
        # Stopped by a process that exited before closing the row.
        await db.transitions.finish(row["id"], "completed", {"stopped": True})
        return "stopped"
    if row["start_at"] > now_iso():
        return "waiting"
    return await _start(config, store, db, row)


async def _start(config: BackboneConfig, store: AgentStore, db: BackboneDB, row: dict) -> str:
    name = row["agent_name"]
    # The launch's operation identity is persisted first: if this process
    # exits mid-launch, the next one can tell the replacement it started
    # from a session someone started by hand (``_recovered_launch``).
    resumed_launch = row["launch_operation_id"]
    operation_id = resumed_launch or uuid.uuid4().hex
    if resumed_launch is None:
        await db.transitions.mark_launching(row["id"], operation_id)
    req = StartRequest(
        name=name,
        runtime=row["runtime"],
        model=row["model"],
        resume=row["resume"],
        operation_id=operation_id,
        expected_registration_id=row.get("registration_id"),
    )
    # Read before the retry: its own "already_running" record would be the newest.
    recovered = await _recovered_launch(db, operation_id) if resumed_launch else None
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
        if recovered is None or not await _session_is_launch(name, operation_id):
            outcome["reason"] = "the session was already running at start time; not started here"
            await db.transitions.finish(row["id"], "failed", outcome)
            return "failed"
        outcome["ready"] = recovered
        outcome["evidence"] = [
            f"launch {operation_id} was started by an earlier backbone process; "
            f"its recorded outcome is '{recovered}'"
        ]
    elif not result.ok or result.ready == "exited":
        outcome["reason"] = "the replacement did not start"
        await db.transitions.finish(row["id"], "failed", outcome)
        return "failed"
    if row["message"]:
        sender = row["requested_by"] or "backbone"
        # Registration replacement uses this same cross-process guard. Hold it
        # through delivery/queueing so the continuation cannot change recipients.
        async with profile_lock(config.data_dir):
            if row.get("registration_id") and not await db.agents.registration_matches(
                name, row["registration_id"]
            ):
                outcome["reason"] = "agent registration changed"
                await db.transitions.finish(row["id"], "failed", outcome)
                return "failed"
            report = await safe_deliver(
                name,
                envelope(sender, row["message"]),
                config,
                db=db,
                delivery_kind="direct_message",
                source=SOURCE,
                sender=sender,
                # One identity per transition: what an earlier process delivered
                # to this conversation before exiting is not delivered again.
                operation_id=uuid.uuid5(uuid.NAMESPACE_URL, f"backbone:transition:{row['id']}").hex,
                once=True,
            )
        outcome["message"] = report.outcome.value
        if report.queue is not None:
            outcome["message_queue"] = report.queue
        if report.queue == "failed":
            outcome["reason"] = (
                "the replacement started but the continuation message was neither "
                "delivered nor stored"
            )
            await db.transitions.finish(row["id"], "failed", outcome)
            return "failed"
    await db.transitions.finish(row["id"], "completed", outcome)
    return "started"


async def _recovered_launch(db: BackboneDB, operation_id: str) -> str | None:
    """What the startup diagnostics say a launch this transition began ended as.

    None when no launch under that identity was recorded (the session is
    someone else's) or it is recorded as failed. ``not_observed`` when the
    launch was requested but its readiness never recorded."""
    records = await db.diagnostics.query(operation_id=operation_id, category="startup", limit=20)
    if not records:
        return None
    # A retry that found the session running records "already_running" under
    # the same operation: that is not a launch outcome. With nothing else,
    # the session was running before this launch.
    codes = [record["code"] for record in records]
    outcomes = [r for r in records if r["code"] not in {"requested", "already_running"}]
    if not outcomes:
        return None if "already_running" in codes else "not_observed"
    # Repeated observations update their first row in place, so id order is
    # not observation order: the latest sighting decides.
    newest = max(outcomes, key=lambda record: record["last_seen_at"])["code"]
    return None if newest in {"failed", "exited"} else newest


async def _session_is_launch(name: str, operation_id: str) -> bool:
    """Whether the running session was started by this launch: every launch
    exports its operation id into the session's environment. A session
    someone started later under the same name carries another, or none."""
    return await query_environment_var(name, launch.OPERATION_ENV_KEY) == operation_id
