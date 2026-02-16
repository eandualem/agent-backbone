"""Agent action log endpoint."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Query

from api.deps import get_config
from api.models import ActionRecord, ListEnvelope
from src.config import BackboneConfig

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["actions"])


@router.get("/actions", response_model=ListEnvelope[ActionRecord])
async def list_actions(
    session: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    config: BackboneConfig = Depends(get_config),
):
    """List agent action log entries from github-actions.jsonl."""
    actions_file = config.agent_state.state_path / "github-actions.jsonl"

    if not actions_file.exists():
        return ListEnvelope(items=[], total=0)

    records: list[ActionRecord] = []
    for line in actions_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        record = ActionRecord(
            ts=data.get("ts", 0.0),
            session=data.get("session", ""),
            action=data.get("action", ""),
            issue=data.get("issue"),
        )
        if session and record.session != session:
            continue
        records.append(record)

    records.sort(key=lambda r: r.ts, reverse=True)
    records = records[:limit]
    return ListEnvelope(items=records, total=len(records))
