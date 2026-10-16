"""Session token history; numeric observations, not tasks or billing claims."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from agent_backbone.api.deps import get_config, get_db
from agent_backbone.config import BackboneConfig
from agent_backbone.services.agents import usage_view
from agent_backbone.services.database import BackboneDB

router = APIRouter(prefix="/api/usage", tags=["usage"])


@router.get("")
async def usage(
    agent: str | None = Query(default=None, max_length=200),
    runtime: str | None = Query(default=None, max_length=100),
    session: str | None = Query(default=None, max_length=300),
    since: str | None = Query(default=None, max_length=100),
    until: str | None = Query(default=None, max_length=100),
    by: Literal["session", "model"] = "session",
    refresh: bool = True,
    reprice: bool = False,
    current_only: bool = False,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    config: BackboneConfig = Depends(get_config),
    db: BackboneDB = Depends(get_db),
):
    """Refresh retained runtime evidence and return a paged view plus full totals."""
    try:
        return await usage_view(
            config,
            db,
            agent=agent,
            runtime=runtime,
            session=session,
            since=since,
            until=until,
            by=by,
            refresh=refresh,
            reprice=reprice,
            current_only=current_only,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
