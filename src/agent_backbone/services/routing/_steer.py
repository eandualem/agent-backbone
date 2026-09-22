"""Steering: hand a working agent guidance for its current task, or refuse.

A steer is a transient hook-context offer — never a queue row and never a
paste. It is accepted only for a session that is working right now on a
runtime whose hook can add context (Claude Code, Codex), written under
``<state_dir>/context/<agent>/<launch_id>/`` so only that session can take
it, and settled by ``settle_steers``: ``handed_off`` once the hook took it,
``not_taken`` when the turn ended first (the hook marks it ``.missed``) or no
tool call took it inside the TTL,
``cancelled`` when the session was replaced. At-most-once handoff; a handoff
is not incorporation.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent_backbone.hooks.backbone_state import (
    clear_steer,
    expire_steer,
    offer_steer,
    steer_key,
    steer_offers,
)
from agent_backbone.services.routing._intelligence import get_session_intelligence
from agent_backbone.services.routing.models import SessionIntelligence
from agent_backbone.services.runtimes import get_runtime
from agent_backbone.services.terminal import query_environment_var

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.database import BackboneDB

log = logging.getLogger(__name__)

STEER_KIND = "steer"
STEER_SOURCE = "api-steer"
STEER_TTL_SECONDS = 300
"""A turn-scale wait for the hook to take the offer; then it is ``not_taken``."""
_SETTLE_GRACE_SECONDS = 15
"""A row is written before its file: do not call a brand-new row cancelled."""


@dataclass(frozen=True)
class SteerReport:
    outcome: str
    """``offered`` | ``refused`` | ``failed``."""
    session: str
    reason: str | None = None
    """Why it was refused: ``offline``, ``not_working``, ``unsupported_runtime``,
    ``no_launch_id``; or why it failed."""
    delivery_id: int | None = None
    operation_id: str | None = None
    launch_id: str | None = None
    evidence: list[str] = field(default_factory=list)


async def steer_agent(
    session_name: str, text: str, config: BackboneConfig, *, db: BackboneDB, sender: str
) -> SteerReport:
    """Offer ``text`` to the agent's current turn, or refuse with the reason."""
    profile = await get_session_intelligence(session_name, config)
    evidence = list(profile.evidence)
    if profile.intelligence == SessionIntelligence.OFFLINE:
        return SteerReport("refused", session_name, "offline", evidence=evidence)
    if profile.intelligence != SessionIntelligence.AGENT_WORKING:
        return SteerReport(
            "refused",
            session_name,
            "not_working",
            evidence=[
                *evidence,
                f"the agent is {profile.intelligence.value}, not working on a task; "
                "send an ordinary message instead",
            ],
        )
    runtime = get_runtime(profile.runtime)
    if not runtime.hook_context:
        return SteerReport(
            "refused",
            session_name,
            "unsupported_runtime",
            evidence=[
                *evidence,
                f"{runtime.id} has no hook that can add context mid-turn; "
                "send an ordinary message instead",
            ],
        )
    launch_id = await query_environment_var(session_name, "BACKBONE_LAUNCH_ID")
    if not launch_id:
        return SteerReport(
            "refused",
            session_name,
            "no_launch_id",
            evidence=[*evidence, "the session was not started by the backbone"],
        )
    operation_id = uuid.uuid4().hex
    envelope = f"[via:backbone from:{sender}] (steer for your current task) {text}"
    delivery_id = await db.deliveries.record(
        issue_number=None,
        target_entity=session_name,
        session_name=session_name,
        outcome="offered",
        source=STEER_SOURCE,
        kind=STEER_KIND,
        preview=envelope,
        operation_id=operation_id,
    )
    try:
        key = steer_key(delivery_id)
        placed = await asyncio.to_thread(
            offer_steer, config.state_dir, session_name, launch_id, key, envelope
        )
    except OSError as exc:
        placed = False
        evidence.append(f"could not write the offer: {type(exc).__name__}")
    if not placed:
        await db.deliveries.settle(delivery_id, "failed", expected="offered")
        return SteerReport(
            "failed",
            session_name,
            "offer_not_written",
            delivery_id,
            operation_id,
            launch_id,
            evidence,
        )
    return SteerReport(
        "offered",
        session_name,
        None,
        delivery_id,
        operation_id,
        launch_id,
        [*evidence, f"offered to launch {launch_id}; the hook hands it over on the next tool call"],
    )


async def settle_steers(config: BackboneConfig, db: BackboneDB) -> dict[str, int]:
    """One tick: record what became of every open steer."""
    summary: dict[str, int] = {}
    offers = await asyncio.to_thread(steer_offers, config.state_dir)
    on_disk: set[int] = set()
    for agent, launch_id, delivery_id, state, age in offers:
        on_disk.add(delivery_id)
        if state == "taken":
            await db.deliveries.settle(delivery_id, "handed_off", expected="offered")
            await asyncio.to_thread(clear_steer, config.state_dir, agent, launch_id, delivery_id)
            summary["handed_off"] = summary.get("handed_off", 0) + 1
        elif state == "missed" or age > STEER_TTL_SECONDS:
            if state == "offered" and not await asyncio.to_thread(
                expire_steer, config.state_dir, agent, launch_id, delivery_id
            ):
                continue  # the hook took it meanwhile: handed_off next tick
            await asyncio.to_thread(clear_steer, config.state_dir, agent, launch_id, delivery_id)
            await db.deliveries.settle(delivery_id, "not_taken", expected="offered")
            summary["not_taken"] = summary.get("not_taken", 0) + 1
    open_rows = await db.deliveries.query(kind=STEER_KIND, outcome="offered", limit=500)
    for row in open_rows:
        if row["id"] in on_disk:
            continue
        if _age_seconds(row.get("created_at")) < _SETTLE_GRACE_SECONDS:
            continue
        # The offer is gone without being taken: the session was replaced.
        if await db.deliveries.settle(row["id"], "cancelled", expected="offered"):
            summary["cancelled"] = summary.get("cancelled", 0) + 1
    return summary


def _age_seconds(created_at: str | None) -> float:
    from agent_backbone.services.database import parse_iso

    if not created_at:
        return float("inf")
    try:
        moment = parse_iso(created_at)
    except ValueError:
        return float("inf")
    from datetime import UTC, datetime

    return (datetime.now(UTC) - moment).total_seconds()
