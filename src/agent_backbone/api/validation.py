"""``backbone message validate`` at the API (docs/owner-confirmed-messages.md).

The caller is identified from its connection, never from anything it sends:
the local process holding the connection, and the agent whose tmux pane that
process runs in. Failed validations are bounded per caller and reported to
the owner once per incident, with a count.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from dataclasses import dataclass, field

from fastapi import Request

from agent_backbone.api.confirmations import refuse
from agent_backbone.config import AgentSpec, BackboneConfig
from agent_backbone.services.integrations import notify_humans
from agent_backbone.services.routing import settle_steers
from agent_backbone.services.terminal import CallerUnknown, caller_sessions

INCIDENT_SECONDS = 600
"""An incident ends after this long without a checked failure."""
MAX_FAILURES = 10
"""Failures checked per incident; after them a caller is refused unchecked."""
NOTICE_SECONDS = 30
"""The owner's notice waits this long after an incident's first failure, to count what follows."""
REFUSALS = {
    "caller_unidentified": (403, "Backbone couldn't tell which agent is calling"),
    "unknown_confirmation": (404, "no confirmation has this id"),
    "wrong_recipient": (403, "this confirmation was delivered to another agent"),
    "revoked": (403, "the confirmation was revoked: the sender's key was reset"),
    "not_delivered": (409, "the confirmation hasn't reached this agent yet"),
    "not_claimed": (409, "the confirmation was never claimed, so there is nothing to close"),
    "claim_window_passed": (
        410,
        "more than 24 hours passed since it was sent; ask the owner to confirm it again",
    ),
    "wrong_workspace": (
        403,
        "it was claimed while this agent was registered in another directory",
    ),
    "grant_closed": (410, "this agent already closed it (--done)"),
    "grant_expired": (410, "the claim lasted 24 hours and has expired"),
    "rate_limited": (
        429,
        "too many failed validations from this caller; wait 10 minutes without another",
    ),
}
_CACHED = frozenset(
    {
        "unknown_confirmation",
        "wrong_recipient",
        "revoked",
        "claim_window_passed",
        "grant_expired",
    }
)
"""Refusals that don't change with time: repeated within an incident without a check."""
_UNIDENTIFIED = ""
_UNIDENTIFIED_LIMIT = (
    "too many validations from callers Backbone couldn't identify; wait 10 minutes without another"
)
_sleep = asyncio.sleep


@dataclass
class _Incident:
    last: float
    checked: int = 0
    reasons: Counter = field(default_factory=Counter)
    refused: dict[str, str] = field(default_factory=dict)


class Validations:
    """Failed validations per caller (an agent, or every caller that couldn't
    be identified): an incident lasts until INCIDENT_SECONDS pass without a
    checked failure."""

    def __init__(self) -> None:
        self._incidents: dict[str, _Incident] = {}
        self._notices: set[asyncio.Task] = set()
        self._locks: dict[str, asyncio.Lock] = {}

    def lock(self, caller: str) -> asyncio.Lock:
        return self._locks.setdefault(caller, asyncio.Lock())

    def _current(self, caller: str, now: float) -> _Incident | None:
        incident = self._incidents.get(caller)
        if incident is not None and now - incident.last >= INCIDENT_SECONDS:
            del self._incidents[caller]
            return None
        return incident

    def spent(self, caller: str, now: float) -> bool:
        """Whether the caller's current incident used up its failed checks."""
        incident = self._current(caller, now)
        return incident is not None and incident.checked >= MAX_FAILURES

    def refused(self, caller: str, confirmation_id: str, now: float) -> str | None:
        """The refusal to repeat without a check, if any."""
        incident = self._current(caller, now)
        if incident is None:
            return None
        if cached := incident.refused.get(confirmation_id):
            return cached
        return "rate_limited" if self.spent(caller, now) else None

    def failed(
        self,
        config: BackboneConfig,
        caller: str,
        confirmation_id: str,
        reason: str,
        now: float,
        *,
        checked: bool = True,
    ) -> None:
        incident = self._current(caller, now)
        if incident is None:
            incident = self._incidents[caller] = _Incident(last=now)
            task = asyncio.get_running_loop().create_task(self._notice(config, caller, incident))
            self._notices.add(task)
            task.add_done_callback(self._notices.discard)
        incident.reasons[reason] += 1
        if checked:
            incident.last = now
            incident.checked += 1
        if reason in _CACHED:
            incident.refused[confirmation_id] = reason

    @staticmethod
    async def _notice(config: BackboneConfig, caller: str, incident: _Incident) -> None:
        await _sleep(NOTICE_SECONDS)
        who = f"agent '{caller}'" if caller else "callers Backbone couldn't identify"
        count = sum(incident.reasons.values())
        reasons = ", ".join(f"{reason} ×{n}" for reason, n in sorted(incident.reasons.items()))
        await notify_humans(
            config,
            f"{count} failed validation{'s' if count != 1 else ''} of owner-confirmed messages "
            f"by {who} ({reasons}). Further failures are counted without another notice until "
            f"{INCIDENT_SECONDS // 60} minutes pass without one.",
        )


async def caller_agent(request: Request, config: BackboneConfig) -> AgentSpec:
    """The registered agent whose pane runs the process on the other end of
    this request's connection; ``CallerUnknown`` when there isn't exactly one."""
    client, server = request.scope.get("client"), request.scope.get("server")
    if not client or not server:
        raise CallerUnknown("the connection has no network address")
    found: dict[str, AgentSpec] = {}
    for sessions in await caller_sessions(tuple(client), tuple(server)):
        agents = {name: spec for name in sessions if (spec := config.agents.get(name))}
        if len(agents) != 1:
            raise CallerUnknown("the calling process doesn't run in one agent's own pane")
        found |= agents
    if len(found) != 1:
        raise CallerUnknown("processes of different agents share the connection")
    return next(iter(found.values()))


async def _check(db, agent: AgentSpec, confirmation_id: str, done: bool):
    return await db.signing.validate(
        confirmation_id,
        agent=agent.name,
        workspace=str(agent.path),
        done=done,
        now=int(time.time()),
    )


async def validate(request: Request, config: BackboneConfig, db, confirmation_id: str, done: bool):
    """Claim, recover or close the calling agent's grant; the confirmed text
    is returned only on a claim or recovery."""
    validations: Validations = request.app.state.validations
    # Identification runs one at a time. Callers that couldn't be identified
    # share one budget, and a caller can't be told apart before the lookup:
    # once that budget is spent, every validation waits out the incident.
    async with validations.lock(_UNIDENTIFIED):
        now = time.monotonic()
        if validations.spent(_UNIDENTIFIED, now):
            validations.failed(
                config, _UNIDENTIFIED, confirmation_id, "rate_limited", now, checked=False
            )
            raise refuse(429, "rate_limited", _UNIDENTIFIED_LIMIT)
        try:
            agent = await caller_agent(request, config)
        except CallerUnknown as exc:
            validations.failed(config, _UNIDENTIFIED, confirmation_id, "caller_unidentified", now)
            status, message = REFUSALS["caller_unidentified"]
            raise refuse(status, "caller_unidentified", f"{message}: {exc}") from exc
    # One check at a time per caller, so a burst can't outrun the bound.
    async with validations.lock(agent.name):
        now = time.monotonic()
        if reason := validations.refused(agent.name, confirmation_id, now):
            validations.failed(config, agent.name, confirmation_id, reason, now, checked=False)
            raise refuse(REFUSALS[reason][0], reason, REFUSALS[reason][1])
        outcome, record = await _check(db, agent, confirmation_id, done)
        if outcome == "not_delivered":
            # A steer the agent's hook just took is recorded on the next
            # settle tick; settle it now rather than refuse the recipient.
            await settle_steers(config, db)
            outcome, record = await _check(db, agent, confirmation_id, done)
        if record is None:
            validations.failed(config, agent.name, confirmation_id, outcome, now)
            raise refuse(REFUSALS[outcome][0], outcome, REFUSALS[outcome][1])
    receipt, grant = record["receipt"], record["grant"]
    result = {
        "confirmation_id": confirmation_id,
        "outcome": outcome,
        "agent": agent.name,
        "grant": {key: grant[key] for key in ("status", "claimed_at", "expires_at", "closed_at")},
    }
    if outcome == "closed":
        return result
    return {
        **result,
        "recovered": outcome == "recovered",
        **{
            key: receipt[key]
            for key in ("sender", "kind", "text", "text_sha256", "source", "confirmed_at")
        },
        "delivered_at": receipt["delivered_at"],
    }
