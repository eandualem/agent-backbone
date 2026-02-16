"""Workflow management endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from api.models import ListEnvelope, WorkflowInfo
from src.workflow_registry import WorkflowRegistry

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["workflows"])


def _get_registry() -> WorkflowRegistry:
    """Get a fresh workflow registry with discovered workflows."""
    registry = WorkflowRegistry()
    registry.discover()
    return registry


@router.get("/workflows", response_model=ListEnvelope[WorkflowInfo])
async def list_workflows():
    """List all available workflow templates."""
    registry = _get_registry()
    items = [
        WorkflowInfo(name=entry.name, description=entry.description, module=entry.module)
        for entry in registry.workflows.values()
    ]
    return ListEnvelope(items=items, total=len(items))


@router.post("/workflows/{name}/run")
async def run_workflow(name: str):
    """Execute a workflow by name."""
    registry = _get_registry()
    entry = registry.get(name)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found")
    try:
        result = await entry.flow_fn()
        return {"ok": True, "workflow": name, "result": str(result)}
    except Exception as e:
        log.exception("Workflow '%s' failed", name)
        raise HTTPException(status_code=500, detail=str(e)) from e
