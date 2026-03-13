"""Entity-to-session resolution — unified resolution logic."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig

from agent_backbone.config import REPO_NAME_PATTERN
from agent_backbone.services.terminal import session_exists

log = logging.getLogger(__name__)


def _repo_reference(issue_title: str) -> str:
    """Extract the repo token embedded in an issue title."""
    match = REPO_NAME_PATTERN.match(issue_title)
    if not match:
        return ""
    return match.group(1).strip()


def _suggested_targets(target: str, config: BackboneConfig) -> list[str]:
    """Best-effort concrete suggestions for an invalid target."""
    prefix = f"{target.lower()}-"
    suggestions = [
        name
        for name, entry in config.registry.entities.items()
        if entry.entity_type == "role-instance" and name.lower().startswith(prefix)
    ]
    return sorted(dict.fromkeys(suggestions))


def _invalid_target_error(target: str, config: BackboneConfig) -> str:
    """Build a validation error for an unknown or invalid issue target."""
    suggestions = _suggested_targets(target, config)
    if suggestions:
        return (
            f"invalid issue target '{target}'; "
            f"use a concrete instance target ({', '.join(suggestions)})"
        )
    return f"invalid issue target '{target}'"


def is_valid_issue_target(target: str, config: BackboneConfig) -> bool:
    """Whether a `for:` issue target is valid under the concrete-target model."""
    entry = config.registry.entities.get(target)
    return bool(
        target in config.entities.skip
        or (entry is not None and entry.entity_type != "role" and entry.session is not None)
        or target in config.registry.repo_names
        or target in {"coding-agent", "jarvis"}
    )


def validate_issue_targets(targets: list[str], config: BackboneConfig) -> None:
    """Reject issue targets that are not concrete, routable identities."""
    invalid = [
        _invalid_target_error(target, config)
        for target in targets
        if not is_valid_issue_target(target, config)
    ]
    if invalid:
        raise ValueError("; ".join(invalid))


async def resolve_entity_sessions(
    target: str,
    config: BackboneConfig,
    issue_title: str = "",
    *,
    use_title_extraction: bool = True,
) -> list[str]:
    """Resolve a target entity to concrete delivery sessions."""
    session = await resolve_entity_session(
        target,
        config,
        issue_title,
        use_title_extraction=use_title_extraction,
    )
    if not session:
        return []
    return [session]


async def resolve_entity_session(
    target: str,
    config: BackboneConfig,
    issue_title: str = "",
    *,
    use_title_extraction: bool = True,
) -> str | None:
    """Resolve a target entity to a tmux session name."""
    if target in config.entities.skip:
        return None

    entry = config.registry.entities.get(target)
    if entry is not None and entry.entity_type == "role":
        return None

    if target in config.registry.sessions_map:
        return config.registry.sessions_map[target]

    if target in config.registry.repo_names:
        if await session_exists(target):
            log.info("Resolved repo name '%s' to session '%s'", target, target)
            return target
        log.info("Repo '%s' recognized but session not running", target)
        return None

    if target == "jarvis":
        if config.jarvis.enabled:
            return "jarvis"
        log.info("Jarvis target disabled (JARVIS_INJECT_URL not set)")
        return None

    if target == "coding-agent" and use_title_extraction:
        repo_ref = _repo_reference(issue_title)
        if repo_ref:
            repo_name = repo_ref.split("/", 1)[-1].strip()
            if repo_name:
                if await session_exists(repo_name):
                    log.info("Resolved coding-agent via title repo '%s'", repo_name)
                    return repo_name
                log.info("coding-agent title repo '%s' has no active session", repo_name)

    fallback = config.entities.fallback.get(target)
    if fallback:
        log.info("Resolved '%s' via fallback '%s'", target, fallback)
        return fallback
    return None
