"""Activity timeline endpoint — merged feed from actions, deliveries, heartbeats."""

from __future__ import annotations

import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Query

from agent_backbone.api.deps import get_config, get_db
from agent_backbone.api.models import ActivityEvent, ListEnvelope
from agent_backbone.config import BackboneConfig
from agent_backbone.services.database import BackboneDB

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["activity"])


def _load_action_events(config: BackboneConfig, limit: int) -> list[ActivityEvent]:
    """Load action events from github-actions.jsonl."""
    actions_file = config.agent_state.state_path / "github-actions.jsonl"
    if not actions_file.exists():
        return []

    events: list[ActivityEvent] = []
    for line in actions_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        issue = data.get("issue")
        issue_str = f" on #{issue}" if issue else ""
        events.append(
            ActivityEvent(
                ts=data.get("ts", 0.0),
                type="action",
                entity=data.get("session", "unknown"),
                summary=f"{data.get('action', 'unknown')}{issue_str}",
            )
        )
    events.sort(key=lambda e: e.ts, reverse=True)
    return events[:limit]


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


@router.get("/activity", response_model=ListEnvelope[ActivityEvent])
@router.get("/activity/timeline", response_model=ListEnvelope[ActivityEvent])
async def get_activity_timeline(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    config: BackboneConfig = Depends(get_config),
    db: BackboneDB = Depends(get_db),
):
    """Merged activity timeline from actions, deliveries, and heartbeats."""
    # Load from all three sources (fetch more than limit to allow merging)
    fetch_limit = limit + offset
    actions = _load_action_events(config, fetch_limit)
    deliveries = await _load_delivery_events(db, fetch_limit)
    heartbeats = await _load_heartbeat_events(db, fetch_limit)

    # Merge and sort
    all_events = actions + deliveries + heartbeats
    all_events.sort(key=lambda e: e.ts, reverse=True)

    # Apply offset and limit
    paginated = all_events[offset : offset + limit]
    return ListEnvelope(items=paginated, total=len(all_events))
