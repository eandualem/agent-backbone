"""Skills and policies as conversation artifacts (docs/conversations.md).

Their sources stay where they are; Backbone reads them when they are listed,
changed through the store, or their conversation or versions are read, and
records an immutable snapshot whenever the content changed.
"""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

from agent_backbone.services.database import BackboneDB
from agent_backbone.skills import NAME_RE, read_store
from agent_backbone.templates import list_templates, template_source

POLICY_STORE = "policies"
"""One policy store per installation: an override and the bundled default
are the same policy."""

_LOCKS = {"skill": asyncio.Lock(), "policy": asyncio.Lock()}
"""Each read and its recording happen together, so an older read can never be
recorded after a newer one and become the latest version."""


def _regular(path: Path) -> str | None:
    """A regular file's text; None for anything else, without waiting on a FIFO."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, encoding="utf-8") as file:
            fd = -1  # the file object owns and closes it now
            return file.read()
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        if fd >= 0:
            os.close(fd)


def _skill(config, name: str) -> str | None:
    """The skill's SKILL.md: a skill's version is that file."""
    return _regular(config.skills.store_path / name / "SKILL.md")


def _policy(config, name: str) -> str | None:
    """The text agents receive; an empty policy is not readable, as at launch."""
    text = _regular(template_source(f"policy:{name}", config.template_dirs))
    return text if text and text.strip() else None


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
    """Observe the whole store: name → ``{artifact_id, status, latest_revision_id}``.
    A name that has never been readable has no identity yet: ``artifact_id`` None."""
    store = _store(config, kind)
    if store is None:
        return {}
    async with _LOCKS[kind]:
        items = {name: _read(config, kind, name) for name in _names(config, kind)}
        observed = await db.conversations.observe(kind, store, items, complete=True)
    unread = {"artifact_id": None, "status": "missing", "latest_revision_id": None}
    return {name: observed.get(name, unread) for name in items}


async def observe_one(config, db: BackboneDB, kind: str, artifact_id: str) -> None:
    """Re-read one identity's source, unless it is retired or its store moved."""
    source = await db.conversations.observed_source(kind, artifact_id)
    store = _store(config, kind)
    if source is None or store is None or source["status"] == "retired":
        return
    if source["source_key"] != f"{store}/{source['name']}":
        return
    async with _LOCKS[kind]:
        items = {source["name"]: _read(config, kind, source["name"])}
        await db.conversations.observe(kind, store, items, complete=False)


async def observe_name(config, db: BackboneDB, kind: str, name: str) -> dict | None:
    """Observe one item by name, after a store operation changed it."""
    store = _store(config, kind)
    if store is None:
        return None
    async with _LOCKS[kind]:
        observed = await db.conversations.observe(
            kind, store, {name: _read(config, kind, name)}, complete=False
        )
    return observed.get(name)
