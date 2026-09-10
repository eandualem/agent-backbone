"""What the skills store would give an agent at its next launch — without touching anything."""

from __future__ import annotations

import os
from pathlib import Path

from agent_backbone.config import AgentSpec, BackboneConfig
from agent_backbone.services.runtimes import get_runtime
from agent_backbone.skills import Skill, read_store, select_skills


def _link_state(link: Path, store: Path) -> str:
    if link.is_symlink():
        try:
            target = Path(os.readlink(link))
        except OSError:
            return "unreadable link"
        target = target if target.is_absolute() else link.parent / target
        try:
            inside = Path(os.path.normpath(target)).resolve().parent == store.resolve()
        except OSError:
            inside = False
        if inside:
            return "linked" if (link / "SKILL.md").is_file() else "linked but broken"
        return "repository's own link wins"
    if link.exists():
        return "repository's own skill wins"
    return "will link"


def skills_preview(spec: AgentSpec, config: BackboneConfig, *, runtime: str | None = None) -> dict:
    """Selection, directories and per-link state for ``spec``'s next start."""
    rt = get_runtime(runtime or spec.runtime)
    store = config.skills.store_path
    view: dict = {
        "name": spec.name,
        "runtime": rt.id,
        "tags": list(spec.tags),
        "store": str(store) if store else None,
        "directories": list(rt.skill_dirs),
        "skills": [],
        "notices": [],
    }
    if store is None:
        view["notices"].append("skills.store is empty: no skills are materialised")
        return view
    entries = read_store(store)
    if not store.is_dir():
        view["notices"].append(f"store {store} does not exist yet")
    for entry in entries:
        if not entry.valid:
            view["notices"].append(f"store skill {entry.name}: {entry.error}")
    selected: list[Skill] = select_skills(entries, spec.tags, spec.name)
    if not rt.skill_dirs:
        view["notices"].append(
            f"{rt.display_name} has no measured skills directory: nothing is materialised"
        )
    for skill in selected:
        links = {
            directory: _link_state(spec.path / directory / skill.name, store)
            for directory in rt.skill_dirs
        }
        view["skills"].append(
            {
                "name": skill.name,
                "tags": list(skill.tags),
                "description": skill.description,
                "path": str(skill.path),
                "links": links,
            }
        )
    return view
