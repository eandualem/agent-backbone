"""Collect identified runtime conversations and project token-first usage views."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from contextlib import aclosing, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

from agent_backbone.config import BackboneConfig
from agent_backbone.services.agents._file_reader import read_state_file
from agent_backbone.services.runtimes import RUNTIMES
from agent_backbone.usage import (
    DEFAULT_PRICES,
    TOKEN_FIELDS,
    UsageEvent,
    estimate,
    timestamp,
    usage_id,
)

if TYPE_CHECKING:
    from agent_backbone.services.database import BackboneDB

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
        known = {s["id"]: s for s in await db.usage.sessions()}
        pending = deque(known.values())
        processed = set()
        unchanged = []
        prices = config.settings.get("usage.prices", DEFAULT_PRICES)
        while pending:
            session = pending.popleft()
            if session["id"] in processed:
                continue
            rt = RUNTIMES.get(session["runtime"])
            if rt is None or not rt.usage_supported:
                continue
            spec = config.agents.get(session["agent_name"])
            env = spec.env if spec else {}
            try:
                source_path = session.get("_discovered_path", session["source_path"])
                path = Path(source_path) if source_path else None
                if not path or not path.is_file():
                    paths = await asyncio.to_thread(rt.usage_paths, session["session_id"], env)
                    if len(paths) == 1:
                        path = paths[0]
                if path is None:
                    continue
                processed.add(session["id"])
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
                detail = batch.error or ("source read incomplete" if not batch.caught_up else "")
                if (
                    not batch.events
                    and session["source_path"] == str(path)
                    and session["offset"] == batch.offset
                    and session["cursor"] == batch.state
                    and session["coverage"] == coverage
                    and session["detail"] == detail
                ):
                    unchanged.append(session["id"])
                else:
                    await db.usage.ingest(
                        session,
                        path=str(path),
                        offset=batch.offset,
                        cursor=batch.state,
                        events=batch.events,
                        coverage=coverage,
                        detail=detail,
                        prices=prices,
                        observed_at=now,
                    )
                children = await asyncio.to_thread(
                    rt.usage_children, path, session["session_id"], env
                )
                for child_id, child_path in children:
                    child_key = usage_id(session["runtime"], child_id)
                    child = known.get(child_key)
                    if (
                        child is None
                        or child["parent_id"] != session["id"]
                        or child["agent_name"] != session["agent_name"]
                    ):
                        await db.usage.remember(
                            session["agent_name"],
                            session["runtime"],
                            child_id,
                            at=now,
                            parent_id=session["id"],
                        )
                        if child is None:
                            child = known[child_key] = {}
                        child.update(await db.usage.session(child_key))
                    if child_key in processed:
                        continue
                    child["_discovered_path"] = str(child_path)
                    pending.append(child)
            except Exception as exc:
                errors.append(f"{session['id']}: {type(exc).__name__}")
                log.warning("Usage collection failed for %s: %s", session["id"], type(exc).__name__)
        await db.usage.observed(unchanged, now)
        return {"enabled": True, "errors": errors, "observed_at": now}


@dataclass
class _Totals:
    counts: dict = field(default_factory=lambda: dict.fromkeys(TOKEN_FIELDS, 0))
    observations: int = 0
    reasoning: int = 0
    priced: int = 0
    usd: Decimal = Decimal(0)

    def add(self, event: dict) -> None:
        for key in TOKEN_FIELDS:
            self.counts[key] += event[key]
        self.observations += 1
        self.reasoning += event.get("reasoning_tokens") or 0
        if event["cost"]["usd"] is not None:
            self.priced += 1
            self.usd += Decimal(event["cost"]["usd"])

    def result(self) -> dict:
        return {
            **self.counts,
            "total_tokens": sum(self.counts.values()),
            "observations": self.observations,
            "reasoning_tokens": self.reasoning,
            "priced_observations": self.priced,
            "estimated_usd": str(self.usd) if self.priced else None,
            "cost_coverage": "complete"
            if self.observations and self.priced == self.observations
            else "partial"
            if self.priced
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
    summaries = {}
    models = {}
    total = _Totals()
    page = []

    def summarize(batch):
        # One bounded batch at a time: JSON and Decimal work must not hold up
        # delivery on the event loop, and full price bases are kept only for the page.
        for raw in batch:
            e = {"session": raw["session"], **json.loads(raw["data"])}
            e["cost"] = (
                estimate(
                    UsageEvent.model_validate_json(raw["data"]),
                    config.settings.get("usage.prices", DEFAULT_PRICES),
                )
                if reprice
                else json.loads(raw["cost"])
            )
            if session and offset <= total.observations < offset + limit:
                page.append(e)
            total.add(e)
            summary = summaries.get(e["session"])
            if summary is None:
                summary = summaries[e["session"]] = {"totals": _Totals(), "models": set()}
            summary["totals"].add(e)
            summary["models"].add(e["model"])
            summary["last_usage_at"] = e["at"]
            if by == "model" and not session:
                if e["model"] not in models:
                    models[e["model"]] = _Totals()
                models[e["model"]].add(e)

    async with aclosing(
        db.usage.event_batches(
            agent=agent,
            runtime=runtime,
            session=session,
            sessions={s["id"] for s in selected} if current_only else None,
            since=since,
            until=until,
        )
    ) as batches:
        async for batch in batches:
            await asyncio.to_thread(summarize, batch)
    rows = []
    for s in selected:
        summary = summaries.get(s["id"])
        if (since or until) and summary is None:
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
                "models": sorted(summary["models"]) if summary else [],
                "last_usage_at": summary["last_usage_at"] if summary else None,
                **(summary["totals"] if summary else _Totals()).result(),
            }
        )
    rows.sort(key=lambda s: (not s["current"], s["agent_name"], s["first_seen"], s["id"]))
    if session:
        total_items = total.observations
        items = page
    elif by == "model":
        items = [{"model": m, **counts.result()} for m, counts in sorted(models.items())]
        total_items = len(items)
        items = items[offset : offset + limit]
    else:
        total_items = len(rows)
        items = rows[offset : offset + limit]
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
    total = total.result()
    total["coverage"] = (
        "unavailable"
        if not total["observations"]
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
        "items": items,
        "total_items": total_items,
        "has_more": offset + limit < total_items,
        "next_offset": offset + limit if offset + limit < total_items else None,
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
