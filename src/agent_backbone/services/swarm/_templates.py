"""Swarm roles use editable common and role templates from templates/swarm/."""

from __future__ import annotations

from pathlib import Path

from agent_backbone.templates import (
    TemplateDirs,
    list_templates,
    read_template,
    render,
    template_path,
    template_source,
)


def template_paths(role: str, dirs: TemplateDirs | None) -> tuple[Path, Path]:
    name = f"swarm:{role}"
    source = template_source(name, dirs)
    if not source.exists() and not source.is_symlink():
        source = template_source("swarm:worker", dirs)
    target = template_path(dirs, name) if dirs is not None else source
    return source, target


def list_brief_templates(dirs: TemplateDirs) -> list[dict]:
    return [
        {"name": row["name"][6:], "source": row["source"], "override": row["path"]}
        for row in list_templates(dirs)
        if row["name"].startswith("swarm:")
    ]


def render_brief(role: str, facts: dict[str, str], *, dirs: TemplateDirs | None = None) -> str:
    common = read_template("swarm:common", dirs)
    source, _ = template_paths(role, dirs)
    body = source.read_text()
    if not body.strip():
        raise ValueError(f"Swarm role template is empty: {source}")
    return render(f"{common}\n{body}".strip() + "\n", facts)
