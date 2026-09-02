"""Role briefs — the instructions injected into every swarm member at launch.

Templates ship with the package (``templates/<role>.md``); a file with the
same name under ``<data_dir>/swarm-templates/`` overrides it. Every brief is
the shared preamble (``common.md``) followed by the role body, rendered with
the swarm's facts. Nothing is ever written into the repository.
"""

from __future__ import annotations

from pathlib import Path

from agent_backbone.templates import load_template, render

_TEMPLATE_DIR = Path(__file__).with_name("templates")
_FALLBACK_ROLE = "worker"


def render_brief(role: str, facts: dict[str, str], *, data_dir: Path | None = None) -> str:
    """The full brief for a role: common preamble + role body, placeholders filled."""
    override = (data_dir / "swarm-templates") if data_dir is not None else None
    common = load_template("common", _TEMPLATE_DIR, override) or ""
    body = (
        load_template(role, _TEMPLATE_DIR, override)
        or load_template(_FALLBACK_ROLE, _TEMPLATE_DIR, override)
        or ""
    )
    return render(f"{common}\n{body}".strip() + "\n", facts)
