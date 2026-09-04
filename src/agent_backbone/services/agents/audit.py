"""The audit trail of remote answers to an agent's prompts.

Every approval or denial that reaches a runtime's dialog from outside the
terminal — the API, a Telegram button — is recorded as an event with who
asked and what was on screen, whichever surface it came from.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.services.database import BackboneDB

log = logging.getLogger(__name__)


async def record_answer(
    db: BackboneDB | None,
    *,
    agent: str,
    runtime: str,
    verb: str,
    by: str,
    evidence: list[str],
) -> None:
    """Record that ``by`` ``verb`` (``approved`` / ``denied``) a prompt on ``agent``."""
    dialog = next((ln for ln in evidence[1:] if ln), "")
    log.info("Permission prompt on '%s' %s by %s", agent, verb, by)
    if db is None:
        return
    try:
        event_id = await db.events.record(
            delivery_id=f"{verb}:{uuid.uuid4().hex}",
            source="backbone",
            event_type="approval" if verb == "approved" else "denial",
            sender=by,
            summary=f"{by} {verb} a {runtime} permission prompt on {agent}: {dialog}",
        )
        if event_id is not None:
            await db.events.mark_processed(event_id, verb)
    except Exception:
        log.exception("Failed to record the %s on %s (non-fatal)", verb, agent)
