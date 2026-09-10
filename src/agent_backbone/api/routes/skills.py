"""The shared skills store: list, add, retag and preview.

Agents call these through the CLI (``backbone skills add …``) so a
sandboxed runtime, which cannot write to the store itself, still has a
sanctioned way to contribute a skill; the backbone does the move and
commits the store's history.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from agent_backbone.api.deps import get_config, registered_agent_or_404
from agent_backbone.api.models import SkillAddRequest, SkillTagsRequest, SkillView
from agent_backbone.services.agents import skills_preview
from agent_backbone.skills import (
    add_skill,
    commit_store,
    is_git_repository,
    parse_skill,
    read_store,
    write_tags,
)

router = APIRouter(prefix="/api", tags=["skills"])


def _store_or_400(config):
    store = config.skills.store_path
    if store is None:
        raise HTTPException(status_code=400, detail="skills.store is empty: the store is disabled")
    return store


@router.get("/skills")
async def list_skills(config=Depends(get_config)):
    """Every store entry, valid or not, with the agents each one reaches."""
    store = config.skills.store_path
    entries = read_store(store) if store else []
    items = []
    for entry in entries:
        reaches = [
            spec.name
            for spec in config.agents
            if entry.valid
            and set(entry.tags) & ({t.lower() for t in spec.tags} | {"all", f"agent:{spec.name}"})
        ]
        items.append({**SkillView.from_skill(entry).model_dump(), "reaches": reaches})
    return {
        "store": str(store) if store else None,
        "git": bool(store and is_git_repository(store)),
        "items": items,
        "total": len(items),
    }


@router.post("/skills", response_model=SkillView)
async def add_to_store(body: SkillAddRequest, config=Depends(get_config)):
    """Move a skill directory into the store and tag it (a move, never a copy)."""
    store = _store_or_400(config)
    try:
        skill = add_skill(
            store, body.path, name=body.name, tags=tuple(body.tags), replace=body.replace
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await commit_store(store, f"add {skill.name} [{' '.join(skill.tags)}] by {body.actor}")
    return SkillView.from_skill(skill)


@router.put("/skills/{name}/tags", response_model=SkillView)
async def set_tags(name: str, body: SkillTagsRequest, config=Depends(get_config)):
    """Replace a store skill's tags."""
    store = _store_or_400(config)
    current = parse_skill(store / name)
    if current.error == "not a directory":
        raise HTTPException(status_code=404, detail=f"unknown skill '{name}'")
    try:
        write_tags(store / name, tuple(body.tags))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    skill = parse_skill(store / name)
    await commit_store(store, f"tag {name} [{' '.join(body.tags)}] by {body.actor}")
    return SkillView.from_skill(skill)


@router.get("/skills/preview/{agent}")
async def preview(agent: str, config=Depends(get_config)):
    """What that agent's next launch links, and where."""
    spec = registered_agent_or_404(config, agent)
    return skills_preview(spec, config)
