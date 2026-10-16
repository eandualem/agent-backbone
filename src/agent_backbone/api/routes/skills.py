"""The shared skills store: list, add, retag and preview.

Agents call these through the CLI (``backbone skills add …``) so a
sandboxed runtime, which cannot write to the store itself, still has a
sanctioned way to contribute a skill; the backbone does the move and
commits the store's history.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from agent_backbone.api.deps import get_config, registered_agent_or_404
from agent_backbone.api.models import (
    SkillAddRequest,
    SkillChangeView,
    SkillTagsRequest,
    SkillView,
)
from agent_backbone.services.agents import skills_preview
from agent_backbone.services.agents.operations import relink_skills
from agent_backbone.skills import (
    add_skill,
    commit_store,
    is_git_repository,
    parse_skill,
    read_store,
    write_tags,
)

router = APIRouter(prefix="/api", tags=["skills"])

_locks: dict[str, asyncio.Lock] = {}
"""Per store: a change and its commit are one step, so each commit holds one change."""


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
            and set(entry.tags)
            & ({t.lower() for t in spec.tags} | {"all", f"agent:{spec.name.lower()}"})
        ]
        items.append({**SkillView.from_skill(entry).model_dump(), "reaches": reaches})
    return {
        "store": str(store) if store else None,
        "git": bool(store and is_git_repository(store)),
        "items": items,
        "total": len(items),
    }


def _inside_a_registered_directory(config, path: str) -> bool:
    """Only an agent workspace may feed the store through the API.

    Every agent holds the API key, so the request cannot prove which agent
    sent it; what it can guarantee is that the directory being *moved* lies
    inside some registered agent's checkout — never the store itself, a
    home directory or anything else the backbone user can reach.
    """
    try:
        source = Path(path).expanduser().resolve()
    except OSError:
        return False
    for spec in config.agents:
        try:
            root = spec.path.resolve()
        except OSError:
            continue
        if root in source.parents:
            return True
    return False


async def _changed(skill, config, failure: str | None) -> SkillChangeView:
    """The skill, after relinking running agents so the change reaches them now."""
    relinked = await relink_skills(config, list(config.agents), skill=skill.name)
    warnings = [f"store history not recorded: {failure}"] if failure else []
    return SkillChangeView(
        **SkillView.from_skill(skill).model_dump(), relinked=relinked, warnings=warnings
    )


@router.post("/skills", response_model=SkillChangeView)
async def add_to_store(body: SkillAddRequest, config=Depends(get_config)):
    """Move a skill directory into the store and tag it (a move, never a copy)."""
    store = _store_or_400(config)
    if not _inside_a_registered_directory(config, body.path):
        raise HTTPException(
            status_code=422,
            detail="the skill must live inside a registered agent's directory; "
            "run `backbone skills add --local` from the terminal for other paths",
        )
    async with _locks.setdefault(str(store), asyncio.Lock()):
        try:
            skill = add_skill(
                store, body.path, name=body.name, tags=tuple(body.tags), replace=body.replace
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        failure = await commit_store(
            store, f"add {skill.name} [{' '.join(skill.tags)}] by {body.actor}"
        )
    return await _changed(skill, config, failure)


@router.put("/skills/{name}/tags", response_model=SkillChangeView)
async def set_tags(name: str, body: SkillTagsRequest, config=Depends(get_config)):
    """Replace a store skill's tags."""
    store = _store_or_400(config)
    current = parse_skill(store / name)
    if current.error == "not a directory":
        raise HTTPException(status_code=404, detail=f"unknown skill '{name}'")
    async with _locks.setdefault(str(store), asyncio.Lock()):
        try:
            write_tags(store / name, tuple(body.tags))
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        skill = parse_skill(store / name)
        failure = await commit_store(store, f"tag {name} [{' '.join(body.tags)}] by {body.actor}")
    return await _changed(skill, config, failure)


@router.get("/skills/preview/{agent}")
async def preview(agent: str, config=Depends(get_config)):
    """What that agent's next launch links, and where."""
    spec = registered_agent_or_404(config, agent)
    return skills_preview(spec, config)
