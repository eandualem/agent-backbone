"""Inspectable startup instructions; launch and preview use the same composition."""

from __future__ import annotations

from pathlib import Path

from agent_backbone.config import AgentSpec, BackboneConfig
from agent_backbone.services.runtimes import get_runtime
from agent_backbone.templates import (
    append_policies,
    brief_source,
    legacy_template_path,
    policy_source,
    render_agent_brief,
)


def instruction_preview(
    spec: AgentSpec, config: BackboneConfig, *, brief_file: Path | None = None
) -> dict:
    """Configuration for the next launch, not a claim about a running conversation."""
    runtime = get_runtime(spec.runtime)
    names = config.launch.policy_names(spec.tags)
    # Swarm creation writes a rendered role brief. Reuse it for later starts
    # and previews, including runtime switches, rather than losing its role.
    if brief_file is None and spec.swarm:
        brief_file = config.data_dir / "swarms" / spec.swarm / f"{spec.name}.md"
    source = brief_file or brief_source(config.data_dir)
    legacy = not brief_file and source == legacy_template_path(config.data_dir, "base")
    enabled = runtime.brief_mode != "none" and (
        brief_file is not None or config.launch.inject_brief
    )
    sources = [
        {
            "name": "swarm" if brief_file else "base",
            "path": str(source),
            "scope": "swarm" if brief_file else "base",
            "applied": enabled,
        }
    ]
    for name in names:
        scopes = (["global"] if name in config.launch.shared_policy else []) + [
            f"tag:{tag}"
            for tag in sorted(set(spec.tags))
            if name in config.launch.tag_policy.get(tag, ())
        ]
        sources.append(
            {
                "name": name,
                "path": str(policy_source(config.data_dir, name)),
                "scope": ", ".join(scopes),
                "applied": enabled,
            }
        )
    content = ""
    if enabled:
        content = (
            append_policies(source.read_text(), config.data_dir, names)
            if brief_file
            else render_agent_brief(
                {"agent_name": spec.name, "repo": spec.repo or "(no GitHub remote)"},
                config.data_dir,
                policy_names=names,
            )
        )
    notices = []
    if brief_file:
        notices.append(
            "This member reuses its saved role brief. Template edits apply to new swarms."
        )
    if not enabled:
        notices.append("Brief injection is disabled for this agent or runtime.")
    if legacy:
        notices.append(
            "Using legacy agent-brief.md; `backbone templates edit base` "
            "saves to templates/base.md."
        )
    if runtime.brief_mode in ("initial_prompt", "message"):
        notices.append("Resuming keeps the existing conversation; the brief is not injected again.")
    notices.append(
        "Project and runtime-owned instructions may also load; they are not managed here."
    )
    return {
        "name": spec.name,
        "runtime": spec.runtime,
        "mode": runtime.brief_mode,
        "enabled": enabled,
        "sources": sources,
        "content": content,
        "notices": notices,
    }
