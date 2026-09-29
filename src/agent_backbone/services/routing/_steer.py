"""Steering: hand a working agent guidance for its current task, or refuse.

A steer is a transient hook-context offer — never a queue row and never a
paste. It is accepted only for a session that is working right now on a
runtime whose hook can add context (Claude Code, Codex, OpenCode), written under
``<state_dir>/context/<agent>/<launch_id>/`` so only that session can take
it, and settled by ``settle_steers``: ``handed_off`` once the hook took it,
``not_taken`` when the turn ended first (the hook marks it ``.missed``) or no
tool call took it inside the TTL,
``cancelled`` when the session was replaced. At-most-once handoff; a handoff
is not incorporation.
"""

from __future__ import annotations

import asyncio
import contextlib
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
from agent_backbone.services.agents import (
    AgentState,
    StateSnapshot,
    agent_state,
    read_state_file,
)
from agent_backbone.services.routing._delivery import revocation_guard
from agent_backbone.services.routing._envelope import envelope as make_envelope
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
    session_name: str,
    text: str,
    config: BackboneConfig,
    *,
    db: BackboneDB,
    sender: str,
    confirmation_id: str | None = None,
    relay: bool = False,
    operation_id: str | None = None,
    claim_token: str | None = None,
) -> SteerReport:
    """Offer ``text`` to the agent's current turn, or refuse with the reason.

    ``confirmation_id`` puts the owner-confirmed marker in the envelope (only
    for a verified confirmation); ``relay`` labels a signed, unconfirmed steer;
    ``operation_id`` ties the offer to its receipt, and ``claim_token`` is
    the request's claim on its one offer: it publishes only while it holds it."""
    # The turn this steer is for, read before the checks: a turn that ends
    # (or ends and another starts) while the offer is written is caught below.
    turn = await asyncio.to_thread(_hook_record, config, session_name)
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
    operation_id = operation_id or uuid.uuid4().hex
    envelope = make_envelope(sender, text, confirmation_id=confirmation_id, relay=relay, steer=True)
    # A confirmed steer is published under the key reset's guard, and only
    # while its confirmation still holds: a reset lands before this or after.
    guard = revocation_guard() if confirmation_id else contextlib.nullcontext()
    async with guard:
        if confirmation_id and await db.signing.is_revoked(operation_id):
            return SteerReport(
                "refused",
                session_name,
                "revoked",
                evidence=[*evidence, "its key was reset: the confirmation no longer holds"],
            )
        if claim_token and not await db.signing.holds_claim(confirmation_id, claim_token):
            return SteerReport(
                "refused",
                session_name,
                "claim_lost",
                evidence=[*evidence, "another request took over this confirmation's offer"],
            )
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
                offer_steer,
                config.state_dir,
                session_name,
                launch_id,
                key,
                envelope,
                {"prompted_at": turn[1], "session_id": turn[2]} if turn is not None else None,
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
    # The hook writes the turn's end before it retires offers, so a turn that
    # ended before the offer existed shows here; its retirement missed the
    # offer, and the next task must not take it. If the retirement came after
    # the write, the offer is already missed. The hook may have taken it
    # within the turn already, which is a handoff.
    if await _turn_ended(config, session_name, turn) and (
        await asyncio.to_thread(
            expire_steer, config.state_dir, session_name, launch_id, delivery_id
        )
        or await asyncio.to_thread(_missed, config, session_name, launch_id, delivery_id)
    ):
        await asyncio.to_thread(clear_steer, config.state_dir, session_name, launch_id, delivery_id)
        await db.deliveries.settle(delivery_id, "not_taken", expected="offered")
        return SteerReport(
            "refused",
            session_name,
            "not_working",
            delivery_id,
            operation_id,
            launch_id,
            [
                *evidence,
                "the task ended while the offer was written; send an ordinary message instead",
            ],
        )
    if turn is None or turn[1] is None:
        evidence.append(
            "the hook has recorded no prompt for this turn, so the offer is not tied to it: "
            "if the turn ends with no hook event, the next task can take it"
        )
    return SteerReport(
        "offered",
        session_name,
        None,
        delivery_id,
        operation_id,
        launch_id,
        [
            *evidence,
            f"offered to launch {launch_id}; the hook hands it over on the next tool call "
            "(on Codex, when a long-running command exits)",
        ],
    )


def _missed(config: BackboneConfig, session_name: str, launch_id: str, delivery_id: int) -> bool:
    """Whether the turn's end already marked this offer missed."""
    return any(
        (launch, key, state) == (launch_id, delivery_id, "missed")
        for _, launch, key, state, _ in steer_offers(config.state_dir, session_name)
    )


_HookRecord = tuple[float, float | None, str | None]
"""``(timestamp, prompted_at, session_id)`` of the hook's latest state record."""


def _hook_record(config: BackboneConfig, session_name: str) -> _HookRecord | None:
    snapshot = read_state_file(config.state_dir, session_name)
    if snapshot is None or snapshot.source != "push":
        return None
    return snapshot.timestamp, snapshot.prompted_at, snapshot.session_id


async def _turn_ended(config: BackboneConfig, session_name: str, turn: _HookRecord | None) -> bool:
    """Whether the hook has recorded the end of the turn marked ``turn``.

    The hook's own records are the receipt, as for a pasted prompt
    (``prompt_hook_after``): it writes the turn's end (idle; blocked by the
    provider when the turn failed there; unknown when the session ends) just
    before it retires offers, and a new prompt starts another turn. A dialog
    within the turn (waiting for a person) is not its end, whatever the
    screen shows; a record older than the check, such as one left from an
    earlier session, says nothing about this turn."""
    snapshot = await asyncio.to_thread(read_state_file, config.state_dir, session_name)
    if snapshot is None or snapshot.source != "push":
        return False  # no hook evidence: the hook's own retirement and the TTL apply
    if turn is not None and snapshot.timestamp == turn[0]:
        return False
    if snapshot.prompted_at != (turn[1] if turn is not None else None):
        return True
    return snapshot.state in (AgentState.IDLE, AgentState.UNKNOWN) or _provider_failed(snapshot)


async def settle_steers(
    config: BackboneConfig, db: BackboneDB, *, read_states: bool = True
) -> dict[str, int]:
    """One tick: record what became of every open steer.

    What the offer files say is recorded first. Then the state of each agent
    that still has an open offer is read once: a turn that failed at the
    provider leaves them ``not_taken``. A caller that only needs a handoff
    recorded passes ``read_states=False``."""
    summary: dict[str, int] = {}
    offers = await asyncio.to_thread(steer_offers, config.state_dir)
    on_disk: set[int] = set()
    waiting: dict[str, list[tuple[str, int]]] = {}
    for agent, launch_id, delivery_id, state, age in offers:
        on_disk.add(delivery_id)
        if state == "taken":
            await db.deliveries.settle(delivery_id, "handed_off", expected="offered")
            await asyncio.to_thread(clear_steer, config.state_dir, agent, launch_id, delivery_id)
            summary["handed_off"] = summary.get("handed_off", 0) + 1
        elif state == "missed" or age > STEER_TTL_SECONDS:
            await _not_taken(config, db, agent, launch_id, delivery_id, state, summary)
        else:
            waiting.setdefault(agent, []).append((launch_id, delivery_id))
    if read_states:
        for agent, open_offers in waiting.items():
            if await _turn_failed(config, agent):
                for launch_id, delivery_id in open_offers:
                    await _not_taken(config, db, agent, launch_id, delivery_id, "offered", summary)
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


async def _not_taken(
    config: BackboneConfig,
    db: BackboneDB,
    agent: str,
    launch_id: str,
    delivery_id: int,
    state: str,
    summary: dict[str, int],
) -> None:
    if state == "offered" and not await asyncio.to_thread(
        expire_steer, config.state_dir, agent, launch_id, delivery_id
    ):
        return  # the hook took it meanwhile: handed_off next tick
    await asyncio.to_thread(clear_steer, config.state_dir, agent, launch_id, delivery_id)
    await db.deliveries.settle(delivery_id, "not_taken", expected="offered")
    summary["not_taken"] = summary.get("not_taken", 0) + 1


def _provider_failed(snapshot: StateSnapshot) -> bool:
    return snapshot.state == AgentState.BLOCKED and snapshot.reason == "provider"


async def _turn_failed(config: BackboneConfig, agent: str) -> bool:
    """Whether the agent's turn failed at the provider. A runtime whose hooks
    miss such a turn (Codex) runs none, so nothing retires its offers; the
    next task's prompt would find them still offered. A state that cannot be
    read changes nothing."""
    try:
        snapshot = await agent_state(config, agent)
    except Exception:
        log.warning("Could not read %s's state to settle its steers", agent, exc_info=True)
        return False
    return _provider_failed(snapshot)


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
