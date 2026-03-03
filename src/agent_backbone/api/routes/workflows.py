"""Workflow management endpoints."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from agent_backbone.api.deps import get_config, get_workflows_service
from agent_backbone.api.models import ListEnvelope, WorkflowCreateRequest, WorkflowInfo
from agent_backbone.config import BackboneConfig
from agent_backbone.services.automation import WorkflowsService

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["workflows"])

_JSON_WORKFLOW_DIR = Path.home() / ".claude" / "state" / "workflows"


@router.get("/workflows", response_model=ListEnvelope[WorkflowInfo])
async def list_workflows(
    workflows_svc: WorkflowsService = Depends(get_workflows_service),
):
    """List all available workflow templates (Prefect + JSON)."""
    registry = workflows_svc.get_registry()
    items = [
        WorkflowInfo(
            name=entry.name,
            description=entry.description,
            module=entry.module,
            source=entry.source,
            last_run=entry.last_run,
            steps=entry.steps,
        )
        for entry in registry.workflows.values()
    ]
    return ListEnvelope(items=items, total=len(items))


@router.post("/workflows", response_model=WorkflowInfo, status_code=201)
async def create_workflow(body: WorkflowCreateRequest):
    """Create a new JSON-defined workflow.

    Stores the workflow as a JSON file in ~/.claude/state/workflows/.
    Returns 409 if a workflow with the same name already exists.
    """
    _JSON_WORKFLOW_DIR.mkdir(parents=True, exist_ok=True)
    file_path = _JSON_WORKFLOW_DIR / f"{body.name}.json"

    if file_path.exists():
        raise HTTPException(status_code=409, detail=f"Workflow '{body.name}' already exists")

    # Validate steps have required fields
    for i, step in enumerate(body.steps):
        if "action" not in step or "session" not in step:
            raise HTTPException(
                status_code=422,
                detail=f"Step {i} missing required 'action' or 'session' field",
            )

    data = {
        "name": body.name,
        "description": body.description,
        "steps": body.steps,
        "last_run": None,
    }
    file_path.write_text(json.dumps(data, indent=2))

    return WorkflowInfo(
        name=body.name,
        description=body.description,
        source="json",
    )


@router.post("/workflows/{name}/run")
async def run_workflow(
    name: str,
    config: BackboneConfig = Depends(get_config),
    workflows_svc: WorkflowsService = Depends(get_workflows_service),
):
    """Execute a workflow by name.

    For Prefect workflows: invokes the flow function directly.
    For JSON workflows: executes steps via the workflow engine.
    """
    registry = workflows_svc.get_registry()
    entry = registry.get(name)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found")

    if entry.source == "json":
        result = await workflows_svc.execute_steps(entry.steps, config)

        # Update last_run in the JSON file
        file_path = _JSON_WORKFLOW_DIR / f"{name}.json"
        if file_path.exists():
            try:
                from datetime import UTC, datetime

                data = json.loads(file_path.read_text())
                data["last_run"] = datetime.now(UTC).isoformat()
                file_path.write_text(json.dumps(data, indent=2))
            except Exception:
                log.warning("Failed to update last_run for workflow '%s'", name)

        return {"ok": result["ok"], "workflow": name, "result": result}

    # Prefect workflow
    try:
        result = await entry.flow_fn()
        return {"ok": True, "workflow": name, "result": str(result)}
    except Exception as e:
        log.exception("Workflow '%s' failed", name)
        raise HTTPException(status_code=500, detail=str(e)) from e
