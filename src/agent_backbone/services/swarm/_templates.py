"""Swarm roles use editable common and role templates from templates/swarm/."""

from __future__ import annotations

from pathlib import Path

from agent_backbone.templates import (
    list_templates,
    read_template,
    render,
    template_path,
    template_source,
)


def template_paths(role: str, data_dir: Path | None) -> tuple[Path, Path]:
    name = f"swarm:{role}"
    source = template_source(name, data_dir)
    if not source.exists() and not source.is_symlink():
        source = template_source("swarm:worker", data_dir)
    target = template_path(data_dir, name) if data_dir is not None else source
    return source, target


def list_brief_templates(data_dir: Path) -> list[dict]:
    return [
        {"name": row["name"][6:], "source": row["source"], "override": row["path"]}
        for row in list_templates(data_dir)
        if row["name"].startswith("swarm:")
    ]


def render_brief(role: str, facts: dict[str, str], *, data_dir: Path | None = None) -> str:
    common = read_template("swarm:common", data_dir)
    source, _ = template_paths(role, data_dir)
    body = source.read_text()
    if not body.strip():
        raise ValueError(f"Swarm role template is empty: {source}")
    return render(f"{common}\n{body}".strip() + "\n", facts)
