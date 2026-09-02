"""Copy-mode recovery on every monitor tick.

tmux copy mode (scrolling, mouse selection) freezes the pane: pasted text
is not seen by the runtime. It is a defect, not an agent state, so the
backbone cancels it wherever it finds it — here on every tick and in the
delivery path right before a paste — and tells the humans when it cannot.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agent_backbone.recent import RecentKeys
from agent_backbone.services.integrations import notify_humans
from agent_backbone.services.terminal import clear_copy_mode

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig

log = logging.getLogger(__name__)

_alerted = RecentKeys(1800)
"""Sessions the humans were told about; one alert per half hour while it persists."""


async def handle_copy_mode_recovery(config: BackboneConfig, active_sessions: set[str]) -> None:
    """Clear copy mode in every managed session; alert if it will not clear."""
    managed = sorted(s for s in active_sessions if s in config.agents)
    _alerted.retain(set(managed))

    for session_name in managed:
        try:
            _, cleared = await clear_copy_mode(session_name)
        except Exception:
            log.exception("Copy-mode check failed for %s (non-fatal)", session_name)
            continue
        if cleared:
            _alerted.forget(session_name)
            continue
        if _alerted.seen(session_name):
            continue
        log.warning("Copy mode persists in %s after cancel attempt", session_name)
        if await notify_humans(
            config,
            f"Copy mode persists in {session_name}; the backbone could not clear it. "
            "Press q in that tmux pane.",
            agent=session_name,
        ):
            _alerted.mark(session_name)  # retried until someone hears it
