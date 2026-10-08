"""What the skills store would give an agent at its next launch — without touching anything."""

from __future__ import annotations

import os
from pathlib import Path

from agent_backbone.config import AgentSpec, BackboneConfig
from agent_backbone.services.runtimes import get_runtime, project_skill_dirs
from agent_backbone.skills import (
    Skill,
    manifest_path,
    parse_skill,
    read_store,
    recorded_links,
    repository_skills,
    select_skills,
)

PLUGIN_NOTICE = "skills that come with the CLI's own plugins are not listed"


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


def _entry(skill_dir: Path, **extra) -> dict:
    skill = parse_skill(skill_dir)
    return {
        "name": skill.name,
        "description": skill.description,
        "error": skill.error,
        "path": str(skill_dir),
        **extra,
    }


def _repository_state(spec: AgentSpec, rt, places: list[str], name: str) -> str:
    """How the agent's CLI reaches one of the repository's own skills."""
    if set(places) & {*rt.skill_read_dirs, *rt.skill_dirs}:
        return "read where it is"
    if not rt.skill_dirs:
        return f"not read by {rt.display_name}"
    rel = f"{rt.skill_dirs[0]}/{name}"
    link = spec.path / rel
    if link.is_symlink() and Path(os.path.normpath(link.parent / os.readlink(link))) == (
        spec.path / places[0] / name
    ):
        return f"linked as {rel}"
    if link.is_symlink() or link.exists():
        return f"not read: {rel} is taken"
    return f"will link as {rel}"


def _repository(spec: AgentSpec, rt, owned: dict[str, list[str]]) -> list[dict]:
    """The repository's own skills, wherever a CLI reads them, and how this one reaches each."""
    return [
        _entry(
            spec.path / places[0] / name,
            directories=places,
            state=_repository_state(spec, rt, places, name),
        )
        for name, places in sorted(owned.items())
    ]


def _skill_dirs_under(folder: Path) -> list[Path]:
    """Every directory below ``folder`` holding a ``SKILL.md``, at any depth.

    Linked directories are followed, as the CLIs follow them, each real
    directory once (a link back up the tree ends there)."""
    found: list[Path] = []
    seen: set[str] = set()
    for root, dirs, files in os.walk(folder, followlinks=True):
        real = os.path.realpath(root)
        if real in seen:
            dirs[:] = []
            continue
        seen.add(real)
        if "SKILL.md" in files and Path(root) != folder:
            found.append(Path(root))
    return sorted(found)


def _user_level(folders: list[Path]) -> list[dict]:
    """The skills in the user-level directories this CLI loads in every project.

    The CLIs search these at any depth (synced and system skills sit nested)."""
    return [
        _entry(skill_dir, directory=str(folder))
        for folder in folders
        if folder.is_dir()
        for skill_dir in _skill_dirs_under(folder)
    ]


def skills_preview(spec: AgentSpec, config: BackboneConfig, *, runtime: str | None = None) -> dict:
    """Every skill ``spec``'s CLI will load at its next start, by source.

    ``skills``: store skills selected by tag, with per-link state;
    ``repository``: the repository's own; ``user``: the CLI's user-level
    directories. Skills from the CLI's own plugins are not listed.
    """
    rt = get_runtime(runtime or spec.runtime)
    store = config.skills.store_path
    user_dirs = rt.user_skill_dirs(dict(spec.env))
    recorded = recorded_links(manifest_path(config.data_dir, spec.name), spec.path)
    owned = repository_skills(spec.path, project_skill_dirs(), store, recorded)
    view: dict = {
        "name": spec.name,
        "runtime": rt.id,
        "tags": list(spec.tags),
        "store": str(store) if store else None,
        "directories": list(rt.skill_dirs),
        "skills": [],
        "repository": _repository(spec, rt, owned),
        "user_directories": [str(folder) for folder in user_dirs],
        "user": _user_level(user_dirs),
        "notices": [PLUGIN_NOTICE] if rt.skill_dirs else [],
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
        places = owned.get(skill.name, [])
        links = {
            directory: (
                f"repository's own {places[0]}/{skill.name} wins"
                if places and directory not in places
                else _link_state(spec.path / directory / skill.name, store)
            )
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
