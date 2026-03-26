"""Governance action execution — frontend governance engine triggers actions via the backbone."""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from agent_backbone.api.deps import get_config, get_db, get_github
from agent_backbone.api.models import (
    GovernanceActionRequest,
    GovernanceActionResponse,
    GovernanceInstanceCreate,
    GovernanceInstanceResponse,
    GovernanceInstanceUpdate,
    GovernanceTrackCreate,
    GovernanceTrackResponse,
    GovernanceTrackUpdate,
    GovernanceLayoutRequest,
    GovernanceLayoutResponse,
    ListEnvelope,
)
from agent_backbone.config import BackboneConfig
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.github import GitHubClient

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/governance", tags=["governance"])


async def _handle_notify_agent(
    params: dict[str, Any],
    config: BackboneConfig,
    db: BackboneDB,
    track_context: dict[str, Any],
) -> dict[str, Any]:
    from agent_backbone.services.routing._delivery import safe_deliver

    session = params.get("session")
    if not session:
        entity = params.get("entity")
        org = track_context.get("org")
        if entity and org:
            session = config.registry.resolve_role_instance(entity, org)
        if not session:
            raise ValueError(
                f"Cannot resolve session: no 'session' param and "
                f"entity={params.get('entity')!r} org={org!r} did not resolve"
            )

    outcome = await safe_deliver(
        session_name=session,
        message=params["message"],
        config=config,
        db=db,
        delivery_kind="direct_message",
    )
    return {"outcome": outcome}


async def _handle_notify_backbone(
    params: dict[str, Any],
    config: BackboneConfig,
    db: BackboneDB,
) -> dict[str, Any]:
    from agent_backbone.services.routing._delivery import safe_deliver

    outcome = await safe_deliver(
        session_name=params["session"],
        message=f"[via:governance] {params['message']}",
        config=config,
        db=db,
        delivery_kind="direct_message",
    )
    return {"outcome": outcome}


async def _handle_notify_elias(
    params: dict[str, Any],
    config: BackboneConfig,
) -> dict[str, Any]:
    from agent_backbone.services.telegram.interface import TelegramService

    telegram_token = os.environ.get("TELEGRAM_TOKEN", "")
    chat_id = config.telegram.notification_chat_id
    result = await TelegramService.send_notification(telegram_token, chat_id, params["message"])
    return {"sent": result}


async def _handle_notify_jarvis(
    params: dict[str, Any],
    config: BackboneConfig,
) -> dict[str, Any]:
    from agent_backbone.jarvis import inject_message

    result = await inject_message(
        config.jarvis.inject_url,
        params["message"],
        sessions_url=config.jarvis.sessions_url,
    )
    return {"sent": result}


async def _handle_auto_comment(
    params: dict[str, Any],
    gh: GitHubClient,
    track_context: dict[str, Any],
) -> dict[str, Any]:
    body = params.get("body") or params.get("message")
    if not body:
        raise ValueError("Either 'body' or 'message' param is required")

    issue_number = params.get("issue_number") or track_context.get("issue_id")
    if not issue_number:
        raise ValueError("Either 'issue_number' param or issue_id in track_context is required")

    repo = params.get("repo") or track_context.get("repo")

    await gh.add_comment(
        issue_number,
        body,
        repo_full_name=repo,
    )
    return {"commented": True}


async def _handle_log(params: dict[str, Any]) -> dict[str, Any]:
    log.info("Governance log action: %s", params.get("message", ""))
    return {"logged": True}


async def _handle_semantic_search() -> dict[str, Any]:
    return {"stub": True, "message": "semantic_search not yet implemented"}


@router.post("/actions", response_model=GovernanceActionResponse)
async def execute_governance_action(
    body: GovernanceActionRequest,
    request: Request,
    config: BackboneConfig = Depends(get_config),
    db: BackboneDB = Depends(get_db),
    gh: GitHubClient = Depends(get_github),
) -> GovernanceActionResponse:
    """Execute a governance action dispatched by the frontend governance engine."""
    action_type = body.action_type
    params = body.params

    handlers = {
        "notify_agent": lambda: _handle_notify_agent(params, config, db, body.track_context),
        "notify_backbone": lambda: _handle_notify_backbone(params, config, db),
        "notify_elias": lambda: _handle_notify_elias(params, config),
        "notify_jarvis": lambda: _handle_notify_jarvis(params, config),
        "auto_comment": lambda: _handle_auto_comment(params, gh, body.track_context),
        "log": lambda: _handle_log(params),
        "semantic_search": lambda: _handle_semantic_search(),
    }

    handler = handlers.get(action_type)
    if handler is None:
        return GovernanceActionResponse(
            ok=False,
            action_type=action_type,
            result={"error": f"Unknown action type: {action_type}"},
        )

    try:
        result = await handler()
    except Exception as exc:
        log.warning("[GOVERNANCE] Action %s failed: %s", action_type, exc, exc_info=True)
        return GovernanceActionResponse(
            ok=False,
            action_type=action_type,
            result={"error": str(exc)},
        )

    # Emit governance event after successful execution
    from agent_backbone.api.governance_events import emit_governance_event

    await emit_governance_event(
        "action.executed",
        context=body.track_context,
        source="governance",
        data={"action_type": action_type, "result": result},
        sio=getattr(request.app.state, "sio", None),
    )

    return GovernanceActionResponse(ok=True, action_type=action_type, result=result)


# --- Track CRUD ---


@router.get("/tracks", response_model=ListEnvelope[GovernanceTrackResponse])
async def list_tracks(db: BackboneDB = Depends(get_db)):
    """List all governance track definitions."""
    tracks = await db.governance.list_tracks()
    return ListEnvelope(items=tracks, total=len(tracks))


@router.get("/tracks/{track_id}", response_model=GovernanceTrackResponse)
async def get_track(track_id: str, db: BackboneDB = Depends(get_db)):
    """Get a specific governance track definition."""
    track = await db.governance.get_track(track_id)
    if track is None:
        raise HTTPException(status_code=404, detail=f"Track '{track_id}' not found")
    return track


@router.post("/tracks", response_model=GovernanceTrackResponse, status_code=201)
async def create_track(
    body: GovernanceTrackCreate,
    db: BackboneDB = Depends(get_db),
):
    """Create a new governance track definition."""
    existing = await db.governance.get_track(body.id)
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Track '{body.id}' already exists")
    return await db.governance.create_track(
        track_id=body.id,
        name=body.name,
        description=body.description,
        definition=body.definition,
    )


@router.put("/tracks/{track_id}", response_model=GovernanceTrackResponse)
async def update_track(
    track_id: str,
    body: GovernanceTrackUpdate,
    db: BackboneDB = Depends(get_db),
):
    """Update a governance track definition."""
    result = await db.governance.update_track(
        track_id,
        name=body.name,
        description=body.description,
        definition=body.definition,
    )
    if result is None:
        raise HTTPException(status_code=404, detail=f"Track '{track_id}' not found")
    return result


@router.delete("/tracks/{track_id}", status_code=204)
async def delete_track(track_id: str, db: BackboneDB = Depends(get_db)):
    """Delete a governance track definition."""
    deleted = await db.governance.delete_track(track_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Track '{track_id}' not found")


# --- Track Instance CRUD ---


@router.get(
    "/instances",
    response_model=ListEnvelope[GovernanceInstanceResponse],
)
async def list_all_instances(db: BackboneDB = Depends(get_db)):
    """List all governance track instances across all tracks."""
    instances = await db.governance.list_all_instances()
    return ListEnvelope(items=instances, total=len(instances))


@router.get(
    "/tracks/{track_id}/instances",
    response_model=ListEnvelope[GovernanceInstanceResponse],
)
async def list_instances(track_id: str, db: BackboneDB = Depends(get_db)):
    """List instances of a governance track."""
    instances = await db.governance.list_instances(track_id)
    return ListEnvelope(items=instances, total=len(instances))


@router.post(
    "/tracks/{track_id}/instances",
    response_model=GovernanceInstanceResponse,
    status_code=201,
)
async def create_instance(
    track_id: str,
    body: GovernanceInstanceCreate,
    db: BackboneDB = Depends(get_db),
):
    """Create a new instance of a governance track."""
    track = await db.governance.get_track(track_id)
    if track is None:
        raise HTTPException(status_code=404, detail=f"Track '{track_id}' not found")
    return await db.governance.create_instance(
        instance_id=body.id,
        track_id=track_id,
        context=body.context,
        current_state=body.current_state,
        status=body.status,
    )


@router.put(
    "/tracks/instances/{instance_id}",
    response_model=GovernanceInstanceResponse,
)
async def update_instance(
    instance_id: str,
    body: GovernanceInstanceUpdate,
    db: BackboneDB = Depends(get_db),
):
    """Update a track instance state."""
    result = await db.governance.update_instance(
        instance_id,
        current_state=body.current_state,
        status=body.status,
        context=body.context,
        history=body.history,
    )
    if result is None:
        raise HTTPException(status_code=404, detail=f"Instance '{instance_id}' not found")
    return result


# --- Track Layouts ---


@router.get("/layouts/{track_id}", response_model=GovernanceLayoutResponse)
async def get_layout(track_id: str, db: BackboneDB = Depends(get_db)):
    """Get graph layout positions for a track."""
    layout = await db.governance.get_layout(track_id)
    if layout is None:
        raise HTTPException(status_code=404, detail=f"Layout for track '{track_id}' not found")
    return layout


@router.post("/layouts/{track_id}", response_model=GovernanceLayoutResponse)
async def upsert_layout(
    track_id: str,
    body: GovernanceLayoutRequest,
    db: BackboneDB = Depends(get_db),
):
    """Create or update graph layout positions for a track."""
    return await db.governance.upsert_layout(track_id, body.positions)
