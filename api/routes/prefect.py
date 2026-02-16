"""Prefect flow runs proxy endpoint."""

from __future__ import annotations

import logging
from datetime import datetime

import httpx
from fastapi import APIRouter, HTTPException, Query

from api.models import FlowRunResponse, ListEnvelope

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["prefect"])

PREFECT_API = "http://localhost:4200/api"

_STATE_MAP = {
    "COMPLETED": "completed",
    "FAILED": "failed",
    "CRASHED": "failed",
    "CANCELLED": "failed",
    "RUNNING": "running",
}


def _map_flow_run(raw: dict) -> FlowRunResponse:
    """Map a Prefect API flow run dict to our response model."""
    state_obj = raw.get("state", {}) or {}
    state_type = (state_obj.get("type") or "").upper()
    mapped_state = _STATE_MAP.get(state_type, "pending")

    start_time = raw.get("start_time")
    end_time = raw.get("end_time")
    duration = None
    if start_time and end_time:
        try:
            st = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            et = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
            duration = (et - st).total_seconds()
        except (ValueError, TypeError):
            pass

    return FlowRunResponse(
        id=str(raw.get("id", "")),
        name=raw.get("name", ""),
        flow_id=str(raw.get("flow_id", "")),
        state=mapped_state,
        start_time=start_time,
        duration=duration,
        detail=state_obj.get("message", ""),
    )


@router.get("/prefect/runs", response_model=ListEnvelope[FlowRunResponse])
async def list_flow_runs(limit: int = Query(default=20, ge=1, le=100)):
    """List recent Prefect flow runs."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{PREFECT_API}/flow_runs/filter",
                json={
                    "sort": "START_TIME_DESC",
                    "limit": limit,
                },
            )
            resp.raise_for_status()
    except (httpx.HTTPError, httpx.ConnectError) as exc:
        log.warning("Prefect API unavailable: %s", exc)
        raise HTTPException(status_code=502, detail="Prefect API unavailable") from exc

    runs = [_map_flow_run(r) for r in resp.json()]
    return ListEnvelope(items=runs, total=len(runs))
