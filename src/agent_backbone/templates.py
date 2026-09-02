"""Markdown templates shipped with the package, overridable from the data directory.

Both the agent brief (``help/``) and the swarm role briefs
(``services/swarm/templates``) are plain markdown with ``{placeholder}``
facts filled in at render time. A file with the same name under the
override directory replaces the shipped one.
"""

from __future__ import annotations

from pathlib import Path


def load_template(name: str, shipped_dir: Path, override_dir: Path | None) -> str | None:
    """The template text: the data-dir override when present, else the shipped file."""
    if override_dir is not None:
        override = override_dir / f"{name}.md"
        if override.is_file():
            return override.read_text()
    shipped = shipped_dir / f"{name}.md"
    return shipped.read_text() if shipped.is_file() else None


def render(text: str, facts: dict[str, str]) -> str:
    """Replace every ``{key}`` in ``text`` with its fact."""
    for key, value in facts.items():
        text = text.replace("{" + key + "}", str(value))
    return text
