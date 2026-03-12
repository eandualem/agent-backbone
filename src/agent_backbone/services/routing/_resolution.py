"""Entity-to-session resolution — unified resolution logic."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig

from agent_backbone.config import REPO_NAME_PATTERN
from agent_backbone.services.terminal import session_exists

log = logging.getLogger(__name__)


def _role_instance_sessions(target: str, issue_title: str, config: BackboneConfig) -> list[str]:
    """Build ordered concrete instance sessions for a role-based entity."""
    entry = config.registry.entities.get(target)
    if not entry or not entry.instances:
        return []

    org_hint = ""
    match = REPO_NAME_PATTERN.match(issue_title)
    if match:
        repo_name = match.group(1)
        if "/" in repo_name:
            org_hint = repo_name.split("/", 1)[0].lower()

    preferred: list[str] = []
    fallback: list[str] = []

    for instance_name, instance in entry.instances.items():
        if not instance.session:
            continue
        if org_hint and (
            instance.organization.lower() == org_hint or instance_name.lower() == org_hint
        ):
            preferred.append(instance.session)
        else:
            fallback.append(instance.session)

    return list(dict.fromkeys(preferred + fallback))


def _role_session_candidates(target: str, issue_title: str, config: BackboneConfig) -> list[str]:
    """Build candidate tmux sessions for a role-based entity."""
    entry = config.registry.entities.get(target)
    if not entry:
        return []

    candidates = _role_instance_sessions(target, issue_title, config)
    if entry.session:
        candidates.append(entry.session)
    candidates.append(target)
    return list(dict.fromkeys(candidates))


async def resolve_entity_sessions(
    target: str,
    config: BackboneConfig,
    issue_title: str = "",
    *,
    use_title_extraction: bool = True,
) -> list[str]:
    """Resolve a target entity to concrete delivery sessions.

    Role-based entities fan out to all configured instance sessions. Other
    targets resolve to at most one session using the existing single-session
    resolution rules.
    """
    if target in config.entities.skip:
        return []

    role_sessions = _role_instance_sessions(target, issue_title, config)
    if role_sessions:
        return role_sessions

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
    """Resolve a target entity to a tmux session name.

    Unified resolution logic replacing duplicated code in dispatcher and lifecycle.

    Named entities map directly via config. 'coding-agent' extracts repo name
    from issue title (when use_title_extraction=True) and checks session existence,
    falling back to config. Entities in the skip set return None.

    Args:
        target: entity name (e.g. "ike", "coding-agent").
        config: backbone configuration.
        issue_title: issue title for repo name extraction.
        use_title_extraction: if False, skip title parsing for coding-agent
            (used by lifecycle where title format differs).
    """
    # Skip set
    if target in config.entities.skip:
        return None

    role_candidates = _role_session_candidates(target, issue_title, config)
    for candidate in role_candidates:
        if await session_exists(candidate):
            return candidate
    if role_candidates:
        return role_candidates[0]

    # Named entity direct mapping
    if target in config.registry.sessions_map:
        return config.registry.sessions_map[target]

    # Coding agent repo name (e.g. "agent-backbone", "lovely-assistant")
    if target in config.registry.repo_names:
        if await session_exists(target):
            log.info("Resolved repo name '%s' to session '%s'", target, target)
            return target
        log.info("Repo '%s' recognized but session not running", target)
        return None

    # Jarvis: HTTP injection target (no tmux session)
    if target == "jarvis":
        if config.jarvis.enabled:
            return "jarvis"
        log.info("Jarvis target disabled (JARVIS_INJECT_URL not set)")
        return None

    # Coding agent resolution
    if target == "coding-agent":
        if use_title_extraction and issue_title:
            match = REPO_NAME_PATTERN.match(issue_title)
            if match:
                repo_name = match.group(1)
                last_segment = repo_name.rsplit("/", 1)[-1] if "/" in repo_name else repo_name
                candidates = list(
                    dict.fromkeys(
                        [repo_name, repo_name.lower(), last_segment, last_segment.lower()]
                    )
                )
                for candidate in candidates:
                    if await session_exists(candidate):
                        log.info(
                            "Resolved coding-agent -> repo session '%s' (from '%s')",
                            candidate,
                            repo_name,
                        )
                        return candidate
                log.info(
                    "No session found for repo '%s' (tried: %s), using fallback",
                    repo_name,
                    candidates,
                )
            else:
                log.info("Could not extract repo name from title: %s", issue_title)

        # Fallback
        fallback = config.entities.fallback.get(target)
        if fallback:
            log.info("Routing coding-agent -> fallback '%s'", fallback)
            return fallback
        return None

    # Unknown entity
    return None
