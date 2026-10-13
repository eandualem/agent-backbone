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
import hashlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from agent_backbone.hooks.backbone_state import CONTEXT_DIR, offer_context
from agent_backbone.services.runtimes import RUNTIMES
from agent_backbone.usage import usage_id

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.agents import StateSnapshot
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


def _offer(state_dir: Path, agent: str, key: str, session_id: str, text: str) -> None:
    # Only the latest note matters: an earlier one not taken yet is superseded.
    for old in (state_dir / CONTEXT_DIR / agent).glob(f"{_KEY_PREFIX}*"):
        if old.name.partition(".")[0] != key:
            old.unlink(missing_ok=True)
    # The hook hands it over only in this conversation (not after a `/clear`).
    turn = state_dir / CONTEXT_DIR / agent / f"{key}.turn"
    turn.parent.mkdir(parents=True, exist_ok=True)
    turn.write_text(json.dumps({"session_id": session_id}), encoding="utf-8")
    offer_context(state_dir, agent, key, text)  # a key already taken is not offered again


def _withdraw(state_dir: Path, agent: str) -> None:
    for offer in (state_dir / CONTEXT_DIR / agent).glob(f"{_KEY_PREFIX}*.md"):
        offer.unlink(missing_ok=True)


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
        for name in states:  # turned off: nothing offered earlier is handed over
            await asyncio.to_thread(_withdraw, config.state_dir, name)
        return []
    noted = []
    for name, snapshot in states.items():
        try:
            if await _note(config, db, name, snapshot, first, step):
                noted.append(name)
        except OSError:  # one agent's unwritable directory never holds up the rest
            log.warning("Context-size note for %s could not be offered", name, exc_info=True)
    return noted


async def _note(
    config: BackboneConfig,
    db: BackboneDB,
    name: str,
    snapshot: StateSnapshot,
    first: int,
    step: int,
) -> bool:
    spec = config.agents.get(name)
    runtime = RUNTIMES.get(snapshot.runtime or "")
    if spec is None or spec.swarm is not None or not snapshot.session_id:
        return False
    if runtime is None or not runtime.hook_context:
        return False  # recorded as unavailable in the capability contract
    identity = usage_id(snapshot.runtime, snapshot.session_id)
    session = await db.usage.find(identity)
    # A source still being read may end in a smaller context (a compaction);
    # ``detail`` names an incomplete read or an error, also on a partial record.
    if session is None or session["coverage"] == "catching_up" or session["detail"]:
        return False
    tokens = await db.usage.latest_context_tokens(identity)
    if tokens is None:
        return False
    if tokens < session["context_noted"]:
        # Compacted since: a note not taken yet would name a size that is gone.
        await asyncio.to_thread(_withdraw, config.state_dir, name)
    if tokens < first:
        return False
    level = first + (tokens - first) // step * step if step > 0 else first
    if level <= session["context_noted"]:
        return False
    key = f"{_KEY_PREFIX}{hashlib.sha256(identity.encode()).hexdigest()[:12]}-{level}"
    # Offered before it is recorded: a failed write is tried again next tick.
    await asyncio.to_thread(
        _offer, config.state_dir, name, key, snapshot.session_id, note_text(tokens)
    )
    await db.usage.note_context(identity, level)
    log.info("Context-size note offered to %s at %d tokens", name, tokens)
    return True
