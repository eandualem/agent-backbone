"""Copy-mode recovery on every monitor tick.

tmux copy mode (scrolling, mouse selection) freezes the pane: pasted text
is not seen by the runtime. It is a defect, not an agent state, so the
backbone cancels it wherever it finds it — here on every tick and in the
delivery path right before a paste — and tells the humans when it cannot.
"""

from __future__ import annotations

import logging
import time

from agent_backbone.config import BackboneConfig
from agent_backbone.services.integrations import notify_humans
from agent_backbone.services.terminal import clear_copy_mode

log = logging.getLogger(__name__)

_ALERT_DEDUP_SECONDS = 1800
_alerted_at: dict[str, float] = {}


async def handle_copy_mode_recovery(config: BackboneConfig, active_sessions: set[str]) -> None:
    """Clear copy mode in every managed session; alert if it will not clear."""
    managed = sorted(s for s in active_sessions if s in config.agents)
    for stale in [s for s in _alerted_at if s not in managed]:
        del _alerted_at[stale]

    for session_name in managed:
        try:
            cleared = await clear_copy_mode(session_name)
        except Exception:
            log.exception("Copy-mode check failed for %s (non-fatal)", session_name)
            continue
        if cleared:
            _alerted_at.pop(session_name, None)
            continue

        last = _alerted_at.get(session_name)
        if last is not None and (time.monotonic() - last) < _ALERT_DEDUP_SECONDS:
            continue
        log.warning("Copy mode persists in %s after cancel attempt", session_name)
        if await notify_humans(
            config,
            f"Copy mode persists in {session_name}; the backbone could not clear it. "
            "Press q in that tmux pane.",
            agent=session_name,
        ):
            _alerted_at[session_name] = time.monotonic()  # retried until someone hears it
