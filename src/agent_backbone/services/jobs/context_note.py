"""Context-size note: tell a long-running agent how large its conversation has become.

Read from what the usage collector already recorded (each request's
``context_tokens`` for the agent's current conversation). The note is offered
through the runtime's hook context, so it reaches the agent on its next turn:
it never starts a turn and never interrupts a busy agent. Each level is noted
at most once per conversation (``usage_sessions.context_noted``), so a
backbone restart never repeats it and a fresh conversation starts over.

Nothing here restarts anything; the agent decides when to start fresh.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from agent_backbone.hooks.backbone_state import CONTEXT_DIR, offer_context
from agent_backbone.services.runtimes import RUNTIMES
from agent_backbone.usage import usage_id

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.database import BackboneDB
    from agent_backbone.services.jobs.monitor import AgentStates

log = logging.getLogger(__name__)

_KEY_PREFIX = "context-size-"


def note_text(tokens: int) -> str:
    return (
        f"[via:backbone context] Your conversation is at {tokens // 1000}k tokens. "
        "When you finish your current task, write your handoff and start a fresh session "
        "(backbone agent restart)."
    )


def _offer(state_dir: Path, agent: str, level: int, text: str) -> None:
    # Only the latest note matters: one not taken yet is superseded, and one
    # taken in an earlier conversation must not block the same level in this one.
    for old in (state_dir / CONTEXT_DIR / agent).glob(f"{_KEY_PREFIX}*"):
        old.unlink(missing_ok=True)
    offer_context(state_dir, agent, f"{_KEY_PREFIX}{level}", text)


async def note_context_size(
    config: BackboneConfig, db: BackboneDB, states: AgentStates
) -> list[str]:
    """Offer the note to each live agent whose conversation crossed a new level.

    The first level is ``usage.context_note_tokens`` (0 disables the note),
    then one every ``usage.context_note_step_tokens``. Swarm members are
    skipped: they end with their swarm. Returns the agents noted."""
    first = int(config.settings.get("usage.context_note_tokens", 200_000))
    step = int(config.settings.get("usage.context_note_step_tokens", 100_000))
    if first <= 0 or not config.settings.get("usage.enabled", True):
        return []
    noted = []
    for name, snapshot in states.items():
        spec = config.agents.get(name)
        runtime = RUNTIMES.get(snapshot.runtime or "")
        if spec is None or spec.swarm is not None or not snapshot.session_id:
            continue
        if runtime is None or not runtime.hook_context:
            continue  # recorded as unavailable in the capability contract
        identity = usage_id(snapshot.runtime, snapshot.session_id)
        tokens = await db.usage.latest_context_tokens(identity)
        if tokens is None or tokens < first:
            continue
        level = first + (tokens - first) // step * step if step > 0 else first
        if not await db.usage.note_context(identity, level):
            continue
        await asyncio.to_thread(_offer, config.state_dir, name, level, note_text(tokens))
        log.info("Context-size note offered to %s at %d tokens", name, tokens)
        noted.append(name)
    return noted
