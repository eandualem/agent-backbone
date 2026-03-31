"""Registry inspection endpoints — entity metadata and repo discovery."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from agent_backbone.api.deps import get_config
from agent_backbone.api.models import ListEnvelope
from agent_backbone.config import BackboneConfig
from agent_backbone.services.registry._loader import build_registry

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/registry", tags=["registry"])


class RegistryEntityResponse(BaseModel):
    """Single entity record from the registry."""

    id: str
    session: str | None
    home: str
    role: str
    figure: str
    label: str
    tier: str
    section: str | None = None
    parent: str | None = None
    managed_org: str | None = Field(default=None, serialization_alias="managedOrg")
    organization: str = ""
    entity_type: str = Field(default="agent", serialization_alias="entityType")
    groups: list[str] = Field(default_factory=list)


class RegistryRepoResponse(BaseModel):
    """Single repo record from the registry."""

    org: str
    name: str
    path: str


@router.get("/entities", response_model=ListEnvelope[RegistryEntityResponse])
async def list_registry_entities(
    config: BackboneConfig = Depends(get_config),
):
    """Return all named entities from the entity registry."""
    items = [
        RegistryEntityResponse(
            id=name,
            session=entry.session,
            home=entry.home,
            role=entry.role,
            figure=entry.figure,
            label=entry.label or name,
            tier=entry.tier,
            section=entry.section,
            parent=entry.parent,
            managed_org=entry.managed_org,
            organization=entry.organization,
            entity_type=entry.entity_type,
            groups=list(entry.groups),
        )
        for name, entry in config.registry.entities.items()
    ]
    return ListEnvelope(items=items, total=len(items))


@router.get("/repos", response_model=ListEnvelope[RegistryRepoResponse])
async def list_registry_repos(
    config: BackboneConfig = Depends(get_config),
):
    """Return all discovered coding repos from the registry."""
    repos = sorted(
        config.registry.repos,
        key=lambda r: (r.org.casefold(), r.name.casefold()),
    )
    items = [
        RegistryRepoResponse(org=r.org, name=r.name, path=r.path)
        for r in repos
    ]
    return ListEnvelope(items=items, total=len(items))


@router.post("/scan", response_model=ListEnvelope[RegistryRepoResponse])
async def scan_registry(
    config: BackboneConfig = Depends(get_config),
):
    """Trigger a filesystem rescan and reload the entity registry."""
    registry_path = Path(config.registry_config.path).expanduser()
    code_base_dir = Path(config.registry_config.code_base_dir).expanduser()

    try:
        new_registry = build_registry(registry_path, code_base_dir)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Registry rebuild failed: {exc}",
        ) from exc

    # Update the mutable EntityRegistry in-place
    config.registry.entities = new_registry.entities
    config.registry.repos = new_registry.repos
    # Invalidate all cached properties
    for attr in (
        "sessions_map", "entity_by_session", "concrete_sessions_map",
        "all_entities", "repo_names", "orgs", "home_by_session",
        "repo_path_by_name", "tracked_sessions",
    ):
        config.registry.__dict__.pop(attr, None)

    repos = sorted(
        config.registry.repos,
        key=lambda r: (r.org.casefold(), r.name.casefold()),
    )
    items = [
        RegistryRepoResponse(org=r.org, name=r.name, path=r.path)
        for r in repos
    ]
    return ListEnvelope(items=items, total=len(items))
