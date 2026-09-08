"""Agent-facing help topics — the backbone explains itself on demand.

The injected agent brief stays short; when an agent needs detail it asks
for a topic (``backbone help swarms`` or ``GET /api/help/swarms``) instead
of reading the backbone's source. Topics ship with the package
in the top-level ``help/`` directory; installed ``<data_dir>/help/`` files
override them. The legacy ``help-topics/`` override directory is still read.
Injected instructions live separately in ``templates/``.

The user documentation (``docs/*.md`` in the repository) ships with the
package too — ``backbone docs getting-started`` — so an agent that installed
the backbone from PyPI can read the reference without a checkout.
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_backbone.templates import bundled_dir

_TOPICS_DIR = bundled_dir("help")
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}$")

_DOCS_DIRS = (bundled_dir("docs"),)


def _docs_dir() -> Path | None:
    return next((d for d in _DOCS_DIRS if d.is_dir()), None)


def _summary(path: Path) -> str:
    """The first non-empty line, without its heading marker."""
    return next((ln.lstrip("# ").strip() for ln in path.read_text().splitlines() if ln.strip()), "")


def list_docs() -> list[dict]:
    """The shipped documentation pages as ``{name, summary}``."""
    docs_dir = _docs_dir()
    if docs_dir is None:
        return []
    return [
        {"name": path.stem, "summary": _summary(path)}
        for path in sorted(docs_dir.glob("*.md"))
        if _NAME_RE.match(path.stem)
    ]


def get_doc(name: str) -> str | None:
    """One documentation page's markdown, or None."""
    docs_dir = _docs_dir()
    if docs_dir is None or not _NAME_RE.match(name):
        return None
    path = docs_dir / f"{name}.md"
    return path.read_text() if path.is_file() else None


def _override_dir(data_dir: Path | None) -> Path | None:
    return (data_dir / "help") if data_dir is not None else None


def list_topics(data_dir: Path | None = None) -> list[dict]:
    """All topics as ``{name, summary}`` — shipped plus data-dir additions."""
    names: dict[str, Path] = {}
    for source in (
        _TOPICS_DIR,
        data_dir / "help-topics" if data_dir else None,
        _override_dir(data_dir),
    ):
        if source is None or not source.is_dir():
            continue
        for path in sorted(source.glob("*.md")):
            if _NAME_RE.match(path.stem):
                names[path.stem] = path  # later sources override
    return [{"name": name, "summary": _summary(path)} for name, path in sorted(names.items())]


def get_topic(name: str, data_dir: Path | None = None) -> str | None:
    """A topic's markdown, or None. Data-dir files override shipped ones."""
    if name == "instructions":
        name = "templates"
    if not _NAME_RE.match(name):
        return None
    for source in (
        _override_dir(data_dir),
        data_dir / "help-topics" if data_dir else None,
        _TOPICS_DIR,
    ):
        if source is not None:
            path = source / f"{name}.md"
            if path.exists() or path.is_symlink():
                return path.read_text()
    return None
