"""Fleet snapshots: save the running agents with their conversations, resume them exactly.

A snapshot records, for every agent whose session is running, the runtime,
model and directory and the session id that agent's *current* session
reported through its hook. A resume starts each saved agent on exactly that
conversation: never the runtime's latest one and never a fresh one. When the
saved id cannot be used, the agent is left stopped and the reason is given,
so the owner decides what happens next.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agent_backbone.services.agents._file_reader import read_state_file
from agent_backbone.services.agents._inference import agent_state
from agent_backbone.services.agents._locks import lifecycle_lock
from agent_backbone.services.agents.models import AgentState
from agent_backbone.services.agents.operations import (
    StartRequest,
    resolve_agent,
    start_resolved,
    stop_agent_session,
)
from agent_backbone.services.runtimes import RUNTIMES
from agent_backbone.services.terminal import session_exists

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.agents.store import AgentStore
    from agent_backbone.services.database import BackboneDB

log = logging.getLogger(__name__)

_BUSY = frozenset({AgentState.BUSY, AgentState.WAITING_FOR_HUMAN})
_PARALLEL_STARTS = 4
"""Resumes launched at once: a fleet does not wait for one readiness check per agent in turn."""


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _counts(entries: list[dict], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry[key]] = counts.get(entry[key], 0) + 1
    return counts


def save_counts(agents: list[dict]) -> dict[str, int]:
    stops = _counts(agents, "stop")
    return {
        "saved": len(agents),
        "resumable": sum(1 for entry in agents if entry["resumable"]),
        "stopped": stops.get("stopped", 0),
        "skipped_busy": stops.get("skipped_busy", 0),
        "stop_failed": stops.get("stop_failed", 0),
    }


async def _entry(config: BackboneConfig, spec, *, stop: bool) -> dict:
    snapshot = await agent_state(config, spec.name)
    # Only the running session's own report: an id an earlier session left in
    # the state file would resume the wrong conversation.
    hook = read_state_file(config.state_dir, spec.name, current_launch=True)
    session_id = hook.session_id if hook is not None else None
    if session_id and hook.runtime not in (None, spec.runtime):
        session_id = None
    reason = None
    if not RUNTIMES[spec.runtime].supports_exact_resume:
        reason = "exact_resume_unsupported"
    elif not session_id:
        reason = "no_session_reported"
    return {
        "name": spec.name,
        "runtime": spec.runtime,
        "model": spec.model,
        "dir": str(spec.path),
        "session_id": session_id,
        "session_reported_at": _iso(hook.timestamp) if session_id and hook.timestamp else None,
        "resumable": reason is None,
        "not_resumable_reason": reason,
        "state_at_save": snapshot.state.value,
        "stop": "pending" if stop else "not_requested",
        "stop_error": None,
    }


async def save_fleet(
    store: AgentStore,
    config: BackboneConfig,
    db: BackboneDB,
    *,
    stop: bool = False,
    force: bool = False,
    note: str | None = None,
    from_entity: str = "",
) -> dict | None:
    """Save every running agent, durably, then stop them when asked.

    Returns the snapshot, or None when no agent is running (nothing is saved,
    so an empty set never becomes the latest). With ``stop``, an agent that is
    busy or waiting for a person is saved but left running unless ``force``;
    the caller's own session is stopped last."""
    running = [
        spec
        for spec in store.agents
        if not spec.inbox_only
        and spec.name != config.backbone.session_name
        and spec.runtime in RUNTIMES
        and await session_exists(spec.name)
    ]
    if not running:
        return None
    agents = [await _entry(config, spec, stop=stop) for spec in running]
    snapshot = await db.fleet.create(
        created_by=from_entity, note=note, stop=stop, force=force, agents=agents
    )
    if stop:
        # The caller last: an agent saving its own fleet keeps running until the rest is done.
        for entry in sorted(agents, key=lambda entry: entry["name"] == from_entity):
            # Checked again right before the stop: an agent may have started
            # working while the others were saved.
            busy = AgentState.parse(entry["state_at_save"]) in _BUSY or (
                (await agent_state(config, entry["name"])).state in _BUSY
            )
            if busy and not force:
                entry["stop"] = "skipped_busy"
                continue
            try:
                stopped = await stop_agent_session(config, entry["name"])
            except ValueError as exc:
                stopped, entry["stop_error"] = False, str(exc)
            entry["stop"] = "stopped" if stopped else "stop_failed"
            if not stopped and entry["stop_error"] is None:
                entry["stop_error"] = "the session could not be stopped"
        await db.fleet.set_agents(snapshot["id"], agents)
        snapshot["agents"] = agents
    return snapshot


def _not_resumed(entry: dict, reason: str, evidence: str) -> dict:
    return {**_outcome(entry), "outcome": "not_resumed", "reason": reason, "evidence": [evidence]}


def _outcome(entry: dict) -> dict:
    return {
        "name": entry["name"],
        "outcome": "",
        "reason": None,
        "session_id": entry.get("session_id"),
        "runtime": entry["runtime"],
        "model": entry.get("model"),
        "ready": None,
        "session_confirmed": None,
        "evidence": [],
    }


async def _resume_one(
    store: AgentStore, config: BackboneConfig, db: BackboneDB, entry: dict, *, wait: bool
) -> dict:
    name = entry["name"]
    session_id = entry.get("session_id")
    if not session_id:
        return _not_resumed(entry, "no_saved_session", "no session id was reported before the save")
    runtime = RUNTIMES.get(entry["runtime"])
    if runtime is None or not runtime.supports_exact_resume:
        return _not_resumed(
            entry,
            "exact_resume_unsupported",
            f"{entry['runtime']} cannot open a session by id",
        )
    # Held from checking the agent to any rollback: a change made meanwhile is
    # either seen by these checks or never undone by the rollback.
    async with lifecycle_lock(name):
        await store.refresh()
        spec = store.agents.get(name)
        if spec is None:
            return _not_resumed(entry, "agent_unknown", f"'{name}' is no longer a registered agent")
        if await session_exists(name):
            return {**_outcome(entry), "outcome": "already_running", "evidence": ["left running"]}
        if spec.runtime != entry["runtime"]:
            return _not_resumed(
                entry,
                "runtime_changed",
                f"saved on {entry['runtime']}; the agent now runs {spec.runtime}",
            )
        if str(spec.path) != entry["dir"]:
            return _not_resumed(
                entry, "dir_changed", f"saved in {entry['dir']}; the agent is now in {spec.path}"
            )
        previous = spec.model
        outcome = await _launch(store, config, db, entry, session_id, wait=wait)
        if outcome["outcome"] != "resumed_known_session" and previous != entry.get("model"):
            # A resume that did not happen leaves the agent's settings as they were.
            with contextlib.suppress(KeyError, ValueError):
                await store.update(name, model=previous)
    return outcome


async def _launch(
    store: AgentStore,
    config: BackboneConfig,
    db: BackboneDB,
    entry: dict,
    session_id: str,
    *,
    wait: bool,
) -> dict:
    name = entry["name"]
    req = StartRequest(name=name, session_id=session_id, wait=wait)
    try:
        if store.agents.get(name).model != entry.get("model"):
            # The saved model, including none: a model set since is not kept.
            await store.update(name, model=entry.get("model"))
        spec = await resolve_agent(store, req)
        result = await start_resolved(store, config, spec, req, db=db)
    except (KeyError, ValueError) as exc:
        return {**_outcome(entry), "outcome": "failed", "evidence": [str(exc)]}
    outcome = {**_outcome(entry), "ready": result.ready, "evidence": list(result.evidence)}
    if result.already_running:
        return {**outcome, "outcome": "already_running", "ready": None}
    if not result.ok or result.ready == "exited":
        return {**outcome, "outcome": "failed"}
    hook = read_state_file(config.state_dir, name, current_launch=True)
    if hook is not None and hook.session_id:
        outcome["session_confirmed"] = hook.session_id == session_id
    return {**outcome, "outcome": "resumed_known_session"}


async def resume_fleet(
    store: AgentStore,
    config: BackboneConfig,
    db: BackboneDB,
    snapshot: dict,
    *,
    from_entity: str = "",
    wait: bool = True,
) -> dict:
    """Start every saved agent on exactly its saved conversation, a few at a time.

    Agents already running are left alone. The run is appended to the snapshot."""
    gate = asyncio.Semaphore(_PARALLEL_STARTS)

    async def one(entry: dict) -> dict:
        async with gate:
            try:
                return await _resume_one(store, config, db, entry, wait=wait)
            except Exception as exc:  # one agent's failure never loses the run's record
                log.exception("Fleet resume of %s failed", entry["name"])
                return {
                    **_outcome(entry),
                    "outcome": "failed",
                    "evidence": [f"{type(exc).__name__}: {exc}"],
                }

    agents = list(await asyncio.gather(*(one(entry) for entry in snapshot["agents"])))
    run = {
        "snapshot_id": snapshot["id"],
        "resumed_at": _iso(datetime.now(UTC).timestamp()),
        "resumed_by": from_entity,
        "agents": agents,
        "counts": _counts(agents, "outcome"),
    }
    await db.fleet.add_resume(snapshot["id"], run)
    return run


def summary(snapshot: dict) -> dict:
    """A snapshot's list entry: who saved it, its counts and its last resume."""
    last = snapshot["resumes"][-1] if snapshot["resumes"] else None
    return {
        **{key: snapshot[key] for key in ("id", "created_at", "created_by", "note", "stop")},
        "counts": save_counts(snapshot["agents"]),
        "last_resume": {"at": last["resumed_at"], "counts": last["counts"]} if last else None,
    }


__all__ = ["resume_fleet", "save_counts", "save_fleet", "summary"]
