"""Skills and policies as conversation artifacts (docs/conversations.md).

Their sources stay where they are; Backbone reads them when they are listed,
changed through the store, or their conversation or versions are read, and
records an immutable snapshot whenever the content changed.
"""

from __future__ import annotations

from agent_backbone.services.database import BackboneDB
from agent_backbone.skills import NAME_RE, read_store
from agent_backbone.templates import list_templates, read_template

POLICY_STORE = "policies"
"""One policy store per installation: an override and the bundled default
are the same policy."""


def _skill(config, name: str) -> str | None:
    """The skill's SKILL.md: a skill's version is that file."""
    try:
        return (config.skills.store_path / name / "SKILL.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _policy(config, name: str) -> str | None:
    try:
        return read_template(f"policy:{name}", config.template_dirs)
    except ValueError:
        return None


def _store(config, kind: str) -> str | None:
    if kind == "policy":
        return POLICY_STORE
    store = config.skills.store_path
    return str(store.resolve()) if store else None


def _read(config, kind: str, name: str) -> str | None:
    return _skill(config, name) if kind == "skill" else _policy(config, name)


def _names(config, kind: str) -> list[str]:
    """Names each kind's own rules accept; ``list_templates`` already applies the policy rule."""
    if kind == "skill":
        return [
            entry.name
            for entry in read_store(config.skills.store_path)
            if NAME_RE.fullmatch(entry.name)
        ]
    return [
        entry["name"].partition(":")[2]
        for entry in list_templates(config.template_dirs)
        if entry["name"].startswith("policy:")
    ]


async def observe_all(config, db: BackboneDB, kind: str) -> dict[str, dict]:
    """Observe the whole store: name → ``{artifact_id, status, latest_revision_id}``."""
    store = _store(config, kind)
    if store is None:
        return {}
    items = {name: _read(config, kind, name) for name in _names(config, kind)}
    return await db.conversations.observe(kind, store, items, complete=True)


async def observe_one(config, db: BackboneDB, kind: str, artifact_id: str) -> None:
    """Re-read one identity's source, unless it is retired or its store moved."""
    source = await db.conversations.observed_source(kind, artifact_id)
    store = _store(config, kind)
    if source is None or store is None or source["status"] == "retired":
        return
    if source["source_key"] != f"{store}/{source['name']}":
        return
    items = {source["name"]: _read(config, kind, source["name"])}
    await db.conversations.observe(kind, store, items, complete=False)


async def observe_name(config, db: BackboneDB, kind: str, name: str) -> dict | None:
    """Observe one item by name, after a store operation changed it."""
    store = _store(config, kind)
    if store is None:
        return None
    observed = await db.conversations.observe(
        kind, store, {name: _read(config, kind, name)}, complete=False
    )
    return observed.get(name)
