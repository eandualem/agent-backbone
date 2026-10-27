"""Shared policies as conversation artifacts: their identities and versions.

Policies are edited where they live (``backbone instructions``); this lists
them with the ``artifact_id`` their conversation uses and serves the snapshots
Backbone took of them.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query

from agent_backbone.api.deps import get_config, get_db
from agent_backbone.api.observed import observe_all, observe_one
from agent_backbone.api.routes.conversations import observed_version, versions
from agent_backbone.templates import policy_source

router = APIRouter(prefix="/api/policies", tags=["policies"])


@router.get("", operation_id="list_policies")
async def list_policies(config=Depends(get_config), db=Depends(get_db)):
    """Every policy in the store, with its conversation identity and latest version.
    ``path`` is the file agents receive it from, as the instruction sources report it:
    the override when there is one, otherwise the bundled default."""
    observed = await observe_all(config, db, "policy")
    items = [
        {"name": name, "path": str(policy_source(config.template_dirs, name))} | item
        for name, item in sorted(observed.items())
    ]
    return {"items": items, "total": len(items)}


@router.get("/{artifact_id}/versions", operation_id="list_policy_versions")
async def policy_versions(
    artifact_id: str = Path(max_length=64),
    before: int | None = Query(default=None, ge=1, le=2**63 - 1),
    limit: int = Query(default=50, ge=1, le=200),
    config=Depends(get_config),
    db=Depends(get_db),
):
    """Snapshots newest first, each taken when Backbone saw the policy change."""
    await observe_one(config, db, "policy", artifact_id)
    return await versions(db, "policy", artifact_id, before, limit)


@router.get("/{artifact_id}/versions/{version_id}", operation_id="get_policy_version")
async def policy_version(
    artifact_id: str = Path(max_length=64),
    version_id: str = Path(max_length=20),
    db=Depends(get_db),
):
    """One snapshot with its content."""
    return await observed_version(db, "policy", artifact_id, version_id)
