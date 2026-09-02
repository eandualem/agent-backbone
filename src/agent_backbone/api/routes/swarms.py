"""Swarm endpoints — create, list, inspect and disband swarms."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from agent_backbone.api.deps import get_agent_store, get_config, get_db
from agent_backbone.services.swarm import (
    SwarmError,
    create_swarm,
    swarm_overview,
    teardown_swarm,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["swarms"])


class SwarmCreateRequest(BaseModel):
    name: str
    issue: str = Field(description="The pre-existing issue the swarm works, as owner/repo#N")
    members: list[str] = Field(
        default_factory=list,
        description="Member specs role[*N][@runtime[/model]]; a coordinator is added if absent",
    )
    initiator: str = Field(default="", description="Agent that initiates the swarm")


class SwarmCreateResponse(BaseModel):
    ok: bool
    name: str
    coordinator: str
    members: list[str]
    branch: str
    worktree: str
    repo: str
    issue_number: int


@router.post("/swarms", response_model=SwarmCreateResponse)
async def create_swarm_endpoint(
    request: Request,
    body: SwarmCreateRequest,
    config=Depends(get_config),
    db=Depends(get_db),
    store=Depends(get_agent_store),
):
    gh = getattr(request.app.state, "github", None)
    try:
        result = await create_swarm(
            config,
            db,
            store,
            gh,
            name=body.name,
            issue_ref=body.issue,
            member_specs=body.members,
            initiator=body.initiator,
        )
    except SwarmError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SwarmCreateResponse(ok=True, **result.__dict__)


@router.get("/swarms")
async def list_swarms_endpoint(db=Depends(get_db), store=Depends(get_agent_store)):
    return {"items": await swarm_overview(db, store)}


@router.delete("/swarms/{name}")
async def disband_swarm_endpoint(
    name: str,
    config=Depends(get_config),
    db=Depends(get_db),
    store=Depends(get_agent_store),
):
    swarm = await db.swarms.get(name)
    if swarm is None:
        raise HTTPException(status_code=404, detail=f"unknown swarm '{name}'")
    if swarm["status"] != "active":
        return {"ok": True, "name": name, "status": swarm["status"], "members": []}
    members = await teardown_swarm(config, db, store, swarm, status="disbanded")
    return {"ok": True, "name": name, "status": "disbanded", "members": members}
