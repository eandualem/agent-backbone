"""Collect identified runtime conversations and project token-first usage views."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from weakref import WeakKeyDictionary

from agent_backbone.config import BackboneConfig
from agent_backbone.services.agents._file_reader import read_state_file
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.runtimes import RUNTIMES
from agent_backbone.usage import (
    DEFAULT_PRICES,
    TOKEN_FIELDS,
    UsageEvent,
    estimate,
    timestamp,
    usage_id,
)

log = logging.getLogger(__name__)
_locks: WeakKeyDictionary = WeakKeyDictionary()


def _registrations(config: BackboneConfig) -> list[dict]:
    records = []
    for path in sorted((config.state_dir / "usage-sessions").glob("*.json")):
        try:
            if path.stat().st_size <= 4096:
                row = json.loads(path.read_text())
                if isinstance(row, dict):
                    records.append({**row, "_registration_path": path})
        except (OSError, ValueError):
            continue
    # Existing sessions are adopted without restarting them or guessing identity
    # from a directory shared by unrelated standalone CLI conversations.
    for spec in config.agents:
        snapshot = read_state_file(config.state_dir, spec.name)
        if snapshot and snapshot.runtime and snapshot.session_id:
            records.append(
                {
                    "agent": spec.name,
                    "runtime": snapshot.runtime,
                    "session_id": snapshot.session_id,
                    "observed_at": snapshot.timestamp or datetime.now(UTC).timestamp(),
                }
            )
    return records


async def collect_usage(config: BackboneConfig, db: BackboneDB) -> dict:
    """Refresh bounded source chunks on query and the existing monitor tick."""
    if not config.settings.get("usage.enabled", True):
        return {"enabled": False, "errors": []}
    lock = _locks.setdefault(db, asyncio.Lock())
    async with lock:
        now = timestamp(datetime.now(UTC).isoformat())
        errors = []
        registrations = await asyncio.to_thread(_registrations, config)
        for row in registrations:
            if row.get("agent") not in config.agents.names or row.get("runtime") not in RUNTIMES:
                continue
            sid = row.get("session_id")
            if not isinstance(sid, str) or not sid or len(sid) > 300:
                continue
            try:
                await db.usage.remember(
                    row["agent"],
                    row["runtime"],
                    sid,
                    at=timestamp(row["observed_at"]),
                    launch_id=row.get("launch_id"),
                )
                # The database has committed this immutable launch identity.
                # Consume the registration; no age-based deletion or new job needed.
                if registration := row.get("_registration_path"):
                    with suppress(OSError):
                        registration.unlink(missing_ok=True)
            except (ValueError, KeyError, TypeError):
                errors.append("invalid or conflicting session registration")
        pending = await db.usage.sessions()
        processed = set()
        prices = config.settings.get("usage.prices", DEFAULT_PRICES)
        while pending:
            session = pending.pop(0)
            if session["id"] in processed:
                continue
            processed.add(session["id"])
            rt = RUNTIMES.get(session["runtime"])
            if rt is None or not rt.usage_supported:
                continue
            spec = config.agents.get(session["agent_name"])
            env = spec.env if spec else {}
            try:
                path = Path(session["source_path"]) if session["source_path"] else None
                if not path or not path.is_file():
                    paths = await asyncio.to_thread(rt.usage_paths, session["session_id"], env)
                    if len(paths) == 1:
                        path = paths[0]
                if path is None:
                    continue
                batch = await asyncio.to_thread(
                    rt.read_usage,
                    path,
                    session["offset"],
                    {**session["cursor"], "_session_id": session["session_id"]},
                )
                coverage = (
                    "unavailable"
                    if batch.error and not batch.offset
                    else "partial"
                    if batch.error or batch.state.get("partial")
                    else "catching_up"
                    if not batch.caught_up
                    else "measured"
                )
                if batch.error:
                    errors.append(f"{session['id']}: {batch.error}")
                await db.usage.ingest(
                    session,
                    path=str(path),
                    offset=batch.offset,
                    cursor=batch.state,
                    events=batch.events,
                    coverage=coverage,
                    detail=batch.error or ("source read incomplete" if not batch.caught_up else ""),
                    prices=prices,
                    observed_at=now,
                )
                children = await asyncio.to_thread(
                    rt.usage_children, path, session["session_id"], env
                )
                for child_id, child_path in children:
                    child_key = await db.usage.remember(
                        session["agent_name"],
                        session["runtime"],
                        child_id,
                        at=now,
                        parent_id=session["id"],
                    )
                    if child_key in processed:
                        continue
                    child = await db.usage.session(child_key)
                    child["source_path"] = str(child_path)
                    pending.append(child)
            except Exception as exc:
                errors.append(f"{session['id']}: {type(exc).__name__}")
                log.warning("Usage collection failed for %s: %s", session["id"], type(exc).__name__)
        return {"enabled": True, "errors": errors, "observed_at": now}


def totals(events: list[dict]) -> dict:
    counts = {key: sum(e[key] for e in events) for key in TOKEN_FIELDS}
    priced = [e for e in events if e["cost"]["usd"] is not None]
    return {
        **counts,
        "total_tokens": sum(counts.values()),
        "observations": len(events),
        "reasoning_tokens": sum(e.get("reasoning_tokens") or 0 for e in events),
        "priced_observations": len(priced),
        "estimated_usd": str(sum((Decimal(e["cost"]["usd"]) for e in priced), Decimal(0)))
        if priced
        else None,
        "cost_coverage": "complete"
        if events and len(priced) == len(events)
        else "partial"
        if priced
        else "unpriced",
    }


async def usage_view(
    config: BackboneConfig,
    db: BackboneDB,
    *,
    agent: str | None = None,
    runtime: str | None = None,
    session: str | None = None,
    since: str | None = None,
    until: str | None = None,
    by: str = "session",
    refresh: bool = True,
    reprice: bool = False,
    current_only: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Filter request timestamps, then aggregate; pagination never changes totals."""
    from agent_backbone.services.agents._inference import agent_state
    from agent_backbone.services.terminal import session_exists

    if since:
        since = timestamp(since)
    if until:
        until = timestamp(until)
    if since and until and since >= until:
        raise ValueError("since must precede until")
    if runtime and runtime not in RUNTIMES:
        raise ValueError("unknown runtime")
    if by not in {"session", "model"} or not 1 <= limit <= 1000 or offset < 0:
        raise ValueError("invalid usage grouping or pagination")
    collection = (
        await collect_usage(config, db)
        if refresh
        else {"enabled": config.settings.get("usage.enabled", True), "errors": []}
    )
    sessions = await db.usage.sessions()
    if session:
        matches = [s for s in sessions if s["id"] == session or s["session_id"] == session]
        if len(matches) != 1:
            raise ValueError("unknown or ambiguous usage session")
        session = matches[0]["id"]
    selected = [
        s
        for s in sessions
        if (not agent or s["agent_name"] == agent)
        and (not runtime or s["runtime"] == runtime)
        and (not session or s["id"] == session)
    ]
    if agent and agent not in config.agents.names and not selected:
        raise ValueError("unknown agent")
    events = await db.usage.events(
        agent=agent, runtime=runtime, session=session, since=since, until=until
    )
    if reprice:
        for e in events:
            e["cost"] = estimate(
                UsageEvent.model_validate(
                    {k: v for k, v in e.items() if k in UsageEvent.model_fields}
                ),
                config.settings.get("usage.prices", DEFAULT_PRICES),
            )
    grouped = {}
    for e in events:
        grouped.setdefault(e["session"], []).append(e)
    current = set()
    for name in {s["agent_name"] for s in selected}:
        if name in config.agents.names and await session_exists(name):
            state = await agent_state(config, name)
            if state.runtime and state.session_id:
                current.add(usage_id(state.runtime, state.session_id))
    if current_only:
        included = set(current)
        while True:
            expanded = included | {s["id"] for s in selected if s["parent_id"] in included}
            if expanded == included:
                break
            included = expanded
        selected = [s for s in selected if s["id"] in included]
        events = [e for e in events if e["session"] in included]
    rows = []
    for s in selected:
        es = grouped.get(s["id"], [])
        if (since or until) and not es:
            continue
        rows.append(
            {
                k: s[k]
                for k in (
                    "id",
                    "agent_name",
                    "runtime",
                    "session_id",
                    "parent_id",
                    "first_seen",
                    "last_seen",
                    "coverage",
                    "detail",
                    "launches",
                )
            }
            | {
                "current": s["id"] in current,
                "models": sorted({e["model"] for e in es}),
                "last_usage_at": max((e["at"] for e in es), default=None),
                **totals(es),
            }
        )
    rows.sort(key=lambda s: (not s["current"], s["agent_name"], s["first_seen"], s["id"]))
    if session:
        items = events
    elif by == "model":
        groups = {}
        for e in events:
            groups.setdefault(e["model"], []).append(e)
        items = [{"model": m, **totals(es)} for m, es in sorted(groups.items())]
    else:
        items = rows
    now_epoch = datetime.now(UTC).timestamp()
    limits = [
        {
            "session": s["id"],
            "agent_name": s["agent_name"],
            "runtime": s["runtime"],
            **s["cursor"]["limits"],
            "age_seconds": max(
                0,
                now_epoch
                - datetime.fromisoformat(
                    s["cursor"]["limits"]["observed_at"].replace("Z", "+00:00")
                ).timestamp(),
            ),
            "windows": [
                {**window, "expired": window.get("resets_at", float("inf")) <= now_epoch}
                for window in s["cursor"]["limits"]["windows"]
            ],
        }
        for s in selected
        if s["cursor"].get("limits")
    ]
    unavailable = [
        {
            "agent_name": spec.name,
            "runtime": spec.runtime,
            "reason": "no identified usage source"
            if RUNTIMES[spec.runtime].usage_supported
            else "runtime usage unavailable",
        }
        for spec in config.agents
        if not session
        and (not agent or spec.name == agent)
        and (not runtime or spec.runtime == runtime)
        and not any(s["agent_name"] == spec.name and s["runtime"] == spec.runtime for s in selected)
    ]
    total = totals(events)
    total["coverage"] = (
        "unavailable"
        if not events
        else "partial"
        if unavailable or any(s["coverage"] != "measured" for s in rows)
        else "measured"
    )
    if total["coverage"] != "measured" and total["cost_coverage"] == "complete":
        total["cost_coverage"] = "partial"
    return {
        "generated_at": timestamp(datetime.now(UTC).isoformat()),
        "collection": collection,
        "sessions": rows,
        "items": items[offset : offset + limit],
        "total_items": len(items),
        "has_more": offset + limit < len(items),
        "next_offset": offset + limit if offset + limit < len(items) else None,
        "totals": total,
        "limits": limits,
        "unavailable": unavailable,
        "by": by,
        "since": since,
        "until": until,
        "repriced": reprice,
        "semantics": (
            "Disjoint input/cache/output tokens. Reasoning is included in output. "
            "Session totals exclude children; each child is a separate row. "
            "Estimates use dated standard API token rates, not subscription bills. "
            "Quota snapshots are not additive."
        ),
    }
