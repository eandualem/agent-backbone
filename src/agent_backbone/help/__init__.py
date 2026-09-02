"""Agent-facing help topics — the backbone explains itself on demand.

The injected agent brief stays short; when an agent needs detail it asks
for a topic (``backbone help swarms`` or ``GET /api/help/swarms``) instead
of reading the backbone's source. Topics ship with the package
(``topics/*.md``); a file with the same name under
``<data_dir>/help-topics/`` overrides it, and new files there become new
topics.
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_backbone.templates import load_template, render

_TOPICS_DIR = Path(__file__).with_name("topics")
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}$")


def _override_dir(data_dir: Path | None) -> Path | None:
    return (data_dir / "help-topics") if data_dir is not None else None


def list_topics(data_dir: Path | None = None) -> list[dict]:
    """All topics as ``{name, summary}`` — shipped plus data-dir additions."""
    names: dict[str, Path] = {}
    for source in (_TOPICS_DIR, _override_dir(data_dir)):
        if source is None or not source.is_dir():
            continue
        for path in sorted(source.glob("*.md")):
            if _NAME_RE.match(path.stem):
                names[path.stem] = path  # later sources override
    topics = []
    for name, path in sorted(names.items()):
        first_line = next(
            (ln.lstrip("# ").strip() for ln in path.read_text().splitlines() if ln.strip()), ""
        )
        topics.append({"name": name, "summary": first_line})
    return topics


def get_topic(name: str, data_dir: Path | None = None) -> str | None:
    """A topic's markdown, or None. Data-dir files override shipped ones."""
    if not _NAME_RE.match(name):
        return None
    return load_template(name, _TOPICS_DIR, _override_dir(data_dir))


def render_agent_brief(facts: dict[str, str], data_dir: Path | None = None) -> str:
    """The common brief injected into every backbone-started agent.

    Complements the project's own instructions (CLAUDE.md still loads);
    ``<data_dir>/agent-brief.md`` overrides the shipped template.
    """
    template = load_template("agent-brief", Path(__file__).parent, data_dir)
    return render(template or "", facts)
