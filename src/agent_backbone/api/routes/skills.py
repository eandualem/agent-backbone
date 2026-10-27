"""The shared skills store: list, add, retag, preview and versions.

Agents call these through the CLI (``backbone skills add …``) so a
sandboxed runtime, which cannot write to the store itself, still has a
sanctioned way to contribute a skill; the backbone does the move and
commits the store's history.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as PathParam

from agent_backbone.api.auth import require_operator
from agent_backbone.api.deps import get_config, get_db, registered_agent_or_404
from agent_backbone.api.models import (
    SkillAddRequest,
    SkillChangeView,
    SkillTagsRequest,
    SkillView,
)
from agent_backbone.api.observed import observe_all, observe_name, observe_one
from agent_backbone.api.routes.conversations import observed_version, versions
from agent_backbone.services.agents import skills_preview
from agent_backbone.services.agents.operations import relink_skills
from agent_backbone.skills import (
    NAME_RE,
    add_skill,
    commit_store,
    is_git_repository,
    parse_skill,
    read_store,
    store_lock,
    write_tags,
)

router = APIRouter(prefix="/api", tags=["skills"])


def _store_or_400(config):
    store = config.skills.store_path
    if store is None:
        raise HTTPException(status_code=400, detail="skills.store is empty: the store is disabled")
    return store


@router.get("/skills")
async def list_skills(config=Depends(get_config), db=Depends(get_db)):
    """Every store entry, valid or not, with the agents each one reaches and
    its conversation ``artifact_id`` (None while it has never been readable)."""
    store = config.skills.store_path
    entries = read_store(store) if store else []
    observed = await observe_all(config, db, "skill")
    items = []
    for entry in entries:
        reaches = [
            spec.name
            for spec in config.agents
            if entry.valid
            and set(entry.tags)
            & ({t.lower() for t in spec.tags} | {"all", f"agent:{spec.name.lower()}"})
        ]
        artifact_id = observed.get(entry.name, {}).get("artifact_id")
        items.append(
            {
                **SkillView.from_skill(entry).model_dump(),
                "reaches": reaches,
                "artifact_id": artifact_id,
            }
        )
    return {
        "store": str(store) if store else None,
        "git": bool(store and is_git_repository(store)),
        "items": items,
        "total": len(items),
    }


def _registered_source_root(config, path: str) -> Path | None:
    """Only an agent workspace may feed the store through the API.

    Every agent holds the API key, so the request cannot prove which agent
    sent it; what it can guarantee is that the directory being *moved* lies
    inside some registered agent's checkout — never the store itself, a
    home directory or anything else the backbone user can reach.
    """
    try:
        source = Path(path).expanduser().resolve()
    except OSError:
        return None
    for spec in config.agents:
        try:
            root = spec.path.resolve()
        except OSError:
            continue
        if root in source.parents:
            return root
    return None


async def _changed(skill, config, db, failure: str | None) -> SkillChangeView:
    """The skill, after relinking running agents so the change reaches them now."""
    await observe_name(config, db, "skill", skill.name)
    relinked = await relink_skills(config, list(config.agents), skill=skill.name)
    warnings = [f"store history not recorded: {failure}"] if failure else []
    return SkillChangeView(
        **SkillView.from_skill(skill).model_dump(), relinked=relinked, warnings=warnings
    )


@router.post("/skills", response_model=SkillChangeView, dependencies=[Depends(require_operator)])
async def add_to_store(body: SkillAddRequest, config=Depends(get_config), db=Depends(get_db)):
    """Move a skill directory into the store and tag it (a move, never a copy)."""
    store = _store_or_400(config)
    async with store_lock(config.data_dir, store):
        # Checked once the lock is held, right before the move: the path may
        # have changed while this request waited.
        root = _registered_source_root(config, body.path)
        if root is None:
            raise HTTPException(
                status_code=422,
                detail="the skill must live inside a registered agent's directory; "
                "run `backbone skills add --local` from the terminal for other paths",
            )
        try:
            skill = add_skill(
                store,
                body.path,
                name=body.name,
                tags=tuple(body.tags),
                replace=body.replace,
                source_root=root,
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        failure = await commit_store(
            store, f"add {skill.name} [{' '.join(skill.tags)}] by {body.actor}"
        )
    return await _changed(skill, config, db, failure)


@router.put(
    "/skills/{name}/tags", response_model=SkillChangeView, dependencies=[Depends(require_operator)]
)
async def set_tags(
    name: str, body: SkillTagsRequest, config=Depends(get_config), db=Depends(get_db)
):
    """Replace a store skill's tags."""
    store = _store_or_400(config)
    if not NAME_RE.fullmatch(name):
        raise HTTPException(status_code=422, detail="invalid skill name")
    async with store_lock(config.data_dir, store):
        try:
            path = store / name
            if (
                path.is_symlink()
                or path.resolve().parent != store.resolve()
                or (path / "SKILL.md").is_symlink()
            ):
                raise ValueError("the skill must be a direct directory in the store")
            current = parse_skill(path)
            if current.error == "not a directory":
                raise HTTPException(status_code=404, detail=f"unknown skill '{name}'")
            write_tags(path, tuple(body.tags))
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        skill = parse_skill(store / name)
        failure = await commit_store(store, f"tag {name} [{' '.join(body.tags)}] by {body.actor}")
    return await _changed(skill, config, db, failure)


@router.get("/skills/preview/{agent}")
async def preview(agent: str, config=Depends(get_config)):
    """What that agent's next launch links, and where."""
    spec = registered_agent_or_404(config, agent)
    return skills_preview(spec, config)


@router.get("/skills/{artifact_id}/versions", operation_id="list_skill_versions")
async def skill_versions(
    artifact_id: str = PathParam(max_length=64),
    before: int | None = Query(default=None, ge=1, le=2**63 - 1),
    limit: int = Query(default=50, ge=1, le=200),
    config=Depends(get_config),
    db=Depends(get_db),
):
    """Snapshots newest first, each taken when Backbone saw the SKILL.md change."""
    await observe_one(config, db, "skill", artifact_id)
    return await versions(db, "skill", artifact_id, before, limit)


@router.get("/skills/{artifact_id}/versions/{version_id}", operation_id="get_skill_version")
async def skill_version(
    artifact_id: str = PathParam(max_length=64),
    version_id: str = PathParam(max_length=20),
    db=Depends(get_db),
):
    """One snapshot with its content."""
    return await observed_version(db, "skill", artifact_id, version_id)
