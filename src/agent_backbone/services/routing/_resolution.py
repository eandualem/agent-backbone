"""Entity-to-session resolution — unified resolution logic."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig

from agent_backbone.config import REPO_NAME_PATTERN
from agent_backbone.services.terminal import session_exists

log = logging.getLogger(__name__)


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
