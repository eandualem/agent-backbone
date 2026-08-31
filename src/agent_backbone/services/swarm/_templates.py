"""Role briefs — the instructions injected into every swarm member at launch.

Templates ship with the package (``templates/<role>.md``); a file with the
same name under ``<data_dir>/swarm-templates/`` overrides it. Every brief is
the shared preamble (``common.md``) followed by the role body, rendered with
the swarm's facts. Nothing is ever written into the repository.
"""

from __future__ import annotations

from pathlib import Path

_TEMPLATE_DIR = Path(__file__).with_name("templates")
_FALLBACK_ROLE = "worker"


def _load(name: str, data_dir: Path | None) -> str | None:
    if data_dir is not None:
        override = data_dir / "swarm-templates" / f"{name}.md"
        if override.is_file():
            return override.read_text()
    shipped = _TEMPLATE_DIR / f"{name}.md"
    return shipped.read_text() if shipped.is_file() else None


def render_brief(role: str, facts: dict[str, str], *, data_dir: Path | None = None) -> str:
    """The full brief for a role: common preamble + role body, placeholders filled."""
    common = _load("common", data_dir) or ""
    body = _load(role, data_dir) or _load(_FALLBACK_ROLE, data_dir) or ""
    text = f"{common}\n{body}".strip() + "\n"
    for key, value in facts.items():
        text = text.replace("{" + key + "}", str(value))
    return text
