"""Activity timeline endpoint — merged feed from deliveries, heartbeats, and telemetry."""

from __future__ import annotations

import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Query

from agent_backbone.api.deps import get_db
from agent_backbone.api.models import ActivityEvent, ListEnvelope
from agent_backbone.services.database import BackboneDB

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["activity"])

_TIMELINE_TELEMETRY_EVENTS = [
    "session.started",
    "session.stopped",
    "task.started",
    "task.completed",
    "runtime.error",
    "tool.error",
    "issue.created",
    "issue.commented",
    "issue.closed",
    "issue.labeled",
    "pr.opened",
    "message.direct_sent",
    "message.direct_delivered",
    "message.direct_queued",
    "agent.divergence_detected",
]


async def _load_delivery_events(db: BackboneDB, limit: int) -> list[ActivityEvent]:
    """Load delivery events from SQLite."""
    rows = await db.query_deliveries(limit=limit)
    events: list[ActivityEvent] = []
    for row in rows:
        try:
            dt = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            ts = dt.timestamp()
        except (ValueError, KeyError):
            ts = 0.0
        issue_num = row.get("issue_number", "?")
        session = row.get("session_name", "?")
        outcome = row.get("outcome", "?")
        events.append(
            ActivityEvent(
                ts=ts,
                type="delivery",
                entity=row.get("target_entity", "unknown"),
                summary=f"#{issue_num} → {session} ({outcome})",
            )
        )
    return events


async def _load_heartbeat_events(db: BackboneDB, limit: int) -> list[ActivityEvent]:
    """Load heartbeat events from SQLite."""
    rows = await db.query_heartbeats(limit=limit)
    events: list[ActivityEvent] = []
    for row in rows:
        try:
            dt = datetime.fromisoformat(row["delivered_at"].replace("Z", "+00:00"))
            ts = dt.timestamp()
        except (ValueError, KeyError):
            ts = 0.0
        events.append(
            ActivityEvent(
                ts=ts,
                type="heartbeat",
                entity=row.get("agent", "unknown"),
                summary=f"heartbeat {row.get('outcome', '?')}",
            )
        )
    return events


async def _load_telemetry_events(db: BackboneDB, limit: int) -> list[ActivityEvent]:
    """Load lightweight timeline projections from normalized telemetry rows."""
    rows = await db.query_activity(limit=limit, events=_TIMELINE_TELEMETRY_EVENTS)
    events: list[ActivityEvent] = []
    for row in rows:
        try:
            ts = float(row.get("ts", 0.0))
        except (TypeError, ValueError):
            ts = 0.0
        events.append(
            ActivityEvent(
                ts=ts,
                type="telemetry",
                entity=row.get("entity") or row.get("session", "unknown"),
                summary=_summarize_telemetry_row(row),
            )
        )
    return events


def _summarize_telemetry_row(row: dict) -> str:
    """Render a concise activity timeline summary from a telemetry row."""
    event = str(row.get("event") or "activity")
    runtime = str(row.get("runtime") or "").strip()
    runtime_prefix = f"{runtime} " if runtime else ""
    data = _parse_row_data(row)

    labels = {
        "session.started": "session started",
        "session.stopped": "session stopped",
        "task.started": "task started",
        "task.completed": "task completed",
        "runtime.error": "runtime error",
        "tool.error": "tool error",
    }
    if event in {"issue.created", "issue.commented", "issue.closed", "issue.labeled"}:
        issue_id = data.get("issue_id")
        suffix = f" #{issue_id}" if issue_id else ""
        return f"{event.replace('.', ' ')}{suffix}"
    if event == "pr.opened":
        issue_id = data.get("issue_id")
        suffix = f" #{issue_id}" if issue_id else ""
        return f"pr opened{suffix}"
    if event == "message.direct_sent":
        return f"direct message sent to {data.get('to_session', '?')}"
    if event == "message.direct_delivered":
        return f"direct message delivered to {data.get('to_session', '?')}"
    if event == "message.direct_queued":
        return f"direct message queued for {data.get('to_session', '?')}"
    if event == "agent.divergence_detected":
        reported = data.get("reported_state", "?")
        observed = data.get("observed_state", "?")
        return f"state divergence {reported} -> {observed}"
    return f"{runtime_prefix}{labels.get(event, event)}".strip()


def _parse_row_data(row: dict) -> dict:
    """Parse the optional JSON payload stored on an activity row."""
    raw = row.get("data")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}


@router.get("/activity", response_model=ListEnvelope[ActivityEvent])
@router.get("/activity/timeline", response_model=ListEnvelope[ActivityEvent])
async def get_activity_timeline(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: BackboneDB = Depends(get_db),
):
    """Merged activity timeline from deliveries, heartbeats, and telemetry."""
    fetch_limit = limit + offset
    deliveries = await _load_delivery_events(db, fetch_limit)
    heartbeats = await _load_heartbeat_events(db, fetch_limit)
    telemetry = await _load_telemetry_events(db, fetch_limit)

    all_events = deliveries + heartbeats + telemetry
    all_events.sort(key=lambda e: e.ts, reverse=True)

    paginated = all_events[offset : offset + limit]
    return ListEnvelope(items=paginated, total=len(all_events))
