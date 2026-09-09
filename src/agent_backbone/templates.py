"""Injected Markdown instructions, separate from on-demand help and reference docs."""

from __future__ import annotations

import re
from pathlib import Path

NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,40}")


def bundled_dir(kind: str) -> Path:
    """Top-level resources in a checkout, packaged resources after install."""
    if kind not in {"templates", "help", "docs"}:
        raise ValueError("unknown resource directory")
    root = Path(__file__).resolve().parents[2]
    if (root / "pyproject.toml").is_file() and (root / kind).is_dir():
        return root / kind
    return Path(__file__).with_name("_resources") / kind


def render(text: str, facts: dict[str, str]) -> str:
    """Replace known placeholders; literal braces in examples are retained."""
    return re.sub(r"\{([a-z_]+)\}", lambda m: str(facts.get(m[1], m[0])), text)


def template_relative(name: str) -> Path:
    if name == "base":
        return Path("base.md")
    category, separator, item = name.partition(":")
    if not separator:
        category, item = "policy", name
    if category not in {"swarm", "policy"} or not NAME_RE.fullmatch(item):
        raise ValueError("template name must be base, swarm:ROLE, or policy:NAME")
    return Path("swarm" if category == "swarm" else "policies") / f"{item}.md"


def template_path(data_dir: Path, name: str) -> Path:
    """The editable installed path; never a file inside Python source."""
    return data_dir / "templates" / template_relative(name)


def legacy_template_path(data_dir: Path, name: str) -> Path:
    relative = template_relative(name)
    if name == "base":
        return data_dir / "agent-brief.md"
    directory = "swarm-templates" if relative.parts[0] == "swarm" else "policies"
    return data_dir / directory / relative.name


def template_source(name: str, data_dir: Path | None = None) -> Path:
    """Canonical override, legacy override, then the bundled default."""
    relative = template_relative(name)
    if data_dir is not None:
        for path in (template_path(data_dir, name), legacy_template_path(data_dir, name)):
            if path.exists() or path.is_symlink():
                return path
    return bundled_dir("templates") / relative


def read_template(name: str, data_dir: Path | None = None) -> str:
    source = template_source(name, data_dir)
    try:
        text = source.read_text()
    except OSError as exc:
        raise ValueError(f"Cannot read template {name!r}: {source}") from exc
    if not text.strip():
        raise ValueError(f"Template {name!r} is empty: {source}")
    return text


def list_templates(data_dir: Path) -> list[dict]:
    names = {"base"}
    for category, directory in (("swarm", "swarm"), ("policy", "policies")):
        legacy = data_dir / ("swarm-templates" if category == "swarm" else "policies")
        for root in (
            bundled_dir("templates") / directory,
            legacy,
            data_dir / "templates" / directory,
        ):
            names.update(
                f"{category}:{p.stem}" for p in root.glob("*.md") if NAME_RE.fullmatch(p.stem)
            )
    return [
        {
            "name": name,
            "source": str(template_source(name, data_dir)),
            "path": str(template_path(data_dir, name)),
            "legacy": template_source(name, data_dir) == legacy_template_path(data_dir, name),
        }
        for name in sorted(names)
    ]


def policy_source(data_dir: Path, name: str) -> Path:
    return template_source(f"policy:{name}", data_dir)


def append_policies(brief: str, data_dir: Path | None, policy_names: tuple[str, ...]) -> str:
    """Required policies compose even with a customized base brief."""
    sections = []
    for name in dict.fromkeys(policy_names):
        if data_dir is None or not NAME_RE.fullmatch(name):
            raise ValueError(f"Invalid shared policy name: {name!r}")
        path = policy_source(data_dir, name)
        try:
            policy = path.read_text().strip()
        except OSError as exc:
            raise ValueError(f"Cannot read configured shared policy {name!r}: {path}") from exc
        if not policy:
            raise ValueError(f"Configured shared policy {name!r} is empty: {path}")
        sections.append(f"## Shared policy: {name}\n\n{policy}\n")
    addition = "\n\n".join(sections)
    if brief.count("{shared_policy}") > 1:
        raise ValueError("Use {shared_policy} at most once in a template")
    if "{shared_policy}" in brief:
        return brief.replace("{shared_policy}", addition)
    return brief + (f"\n\n{addition}" if addition else "")


def render_agent_brief(
    facts: dict[str, str], data_dir: Path | None = None, *, policy_names: tuple[str, ...] = ()
) -> str:
    return append_policies(render(read_template("base", data_dir), facts), data_dir, policy_names)


def brief_source(data_dir: Path) -> Path:
    return template_source("base", data_dir)
