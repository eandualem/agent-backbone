"""Safe delivery — the one function through which text reaches an agent.

Decision order:

1. Issue queue gate (issue kind only): already delivered? older delivered issue
   still unacknowledged?
2. Claim the issue delivery atomically (issue kind only).
3. Session intelligence: offline / waiting_for_human / agent_working /
   human_typing / settling / ready / unknown.
4. Paste + submit through the runtime adapter.

Everything that cannot be delivered now is queued (except issue deliveries
during ``settling``, which the retry job re-attempts) and every attempt is
recorded with its kind, repository and outcome.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from functools import wraps
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

from agent_backbone.models import BLOCKED_OUTCOMES, SUCCESS_OUTCOMES, DeliveryOutcome
from agent_backbone.services.routing._intelligence import get_session_intelligence
from agent_backbone.services.routing.models import SessionIntelligence
from agent_backbone.services.runtimes import send_message

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.database import BackboneDB

log = logging.getLogger(__name__)

_BYPASSABLE = frozenset({SessionIntelligence.HUMAN_TYPING, SessionIntelligence.SETTLING})
"""Blocking conditions ``priority`` may push through. Busy and waiting never are."""
_ACTIVE_ISSUE_CONDITIONS = frozenset(
    {SessionIntelligence.AGENT_WORKING, SessionIntelligence.WAITING_FOR_HUMAN}
)
"""Conditions under which a comment on the agent's *current* issue still goes in."""


@dataclass(frozen=True)
class DeliveryReport:
    """What ``deliver`` did: the outcome, and — when it could not deliver —
    whether the message is now in the queue.

    ``queue`` is ``stored`` (a new row), ``already_queued`` (the same message
    from this sender was already waiting; nothing added), ``failed`` (the
    database refused it — the message is NOT held anywhere) or None (nothing
    needed queueing: delivered, or a kind that is never queued).
    """

    outcome: DeliveryOutcome
    queue: str | None = None

    @property
    def queued(self) -> bool:
        """True only when a row for this message exists in the queue."""
        return self.queue in ("stored", "already_queued")


_session_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def _serialized(fn: Callable[..., Awaitable[DeliveryReport]]):
    """One gate/paste/record transaction per session; idle locks are released.

    Each caller holds its lock strongly while waiting or delivering. The weak
    cache keeps unrelated sessions concurrent without retaining forgotten names.
    """

    @wraps(fn)
    async def locked(session_name: str, *args, **kwargs) -> DeliveryReport:
        lock = _session_locks.setdefault(session_name, asyncio.Lock())
        async with lock:
            return await fn(session_name, *args, **kwargs)

    return locked


def queue_detail(report: DeliveryReport, session_name: str, expiry_minutes: int) -> str:
    """One plain sentence about what happened, for people and agents alike."""
    if report.outcome == DeliveryOutcome.DELIVERED:
        return f"Delivered to {session_name}."
    why = report.outcome.value.replace("_", " ")
    if report.queue == "stored":
        return (
            f"Queued: {session_name} is {why}; the message is stored and will be delivered "
            f"when the agent is ready (it expires after {expiry_minutes} minutes)."
        )
    if report.queue == "already_queued":
        return (
            f"Already in the queue: the same message from you is waiting for "
            f"{session_name}. It was not added again."
        )
    if report.queue == "failed":
        return (
            f"Not delivered and not queued: {session_name} is {why} and the message "
            "could not be stored. Send it again later."
        )
    return f"Not delivered: {session_name} is {why}. This kind of message is not queued."


def _comment_matches_active_issue(
    repo: str, issue_number: int | None, current_repo: str | None, current_issue: int | None
) -> bool:
    if issue_number is None or current_issue is None or issue_number != current_issue:
        return False
    # An unknown repository on either side is not a match: other/repo#42 must
    # not slip past busy protection because the agent works on own/repo#42.
    return bool(repo and current_repo and repo.casefold() == current_repo.casefold())


async def is_acknowledged(
    db: BackboneDB, repo: str, issue_number: int, target_entity: str, session_name: str
) -> bool:
    """Whether the target (or the session delivering for it) acknowledged the issue."""
    if await db.acks.exists(issue_number, target_entity, repo=repo):
        return True
    return session_name != target_entity and await db.acks.exists(
        issue_number, session_name, repo=repo
    )


async def _has_successful_issue_delivery(
    db: BackboneDB, repo: str, issue_number: int, session_name: str
) -> bool:
    rows = await db.deliveries.query(
        issue_number=issue_number, session_name=session_name, limit=25, repo=repo, kind="issue"
    )
    return any((row.get("outcome") or "") in SUCCESS_OUTCOMES for row in rows)


async def _get_unacknowledged_gate_issue(
    db: BackboneDB,
    session_name: str,
    repo: str,
    current_issue: int,
    queue_scope: Collection[tuple[str, int]] | None = None,
) -> tuple[str, int] | None:
    """The most recent successfully delivered issue still awaiting acknowledgment."""
    scope = {(r.casefold(), n) for r, n in (queue_scope or ())}
    rows = await db.deliveries.query(session_name=session_name, limit=100, kind="issue")
    for row in rows:
        issue_number = row.get("issue_number")
        target_entity = row.get("target_entity")
        row_repo = row.get("repo") or ""
        if not isinstance(issue_number, int) or not isinstance(target_entity, str):
            continue
        if scope and (row_repo.casefold(), issue_number) not in scope:
            continue
        if issue_number == current_issue and row_repo.casefold() == repo.casefold():
            continue
        if (row.get("outcome") or "") not in SUCCESS_OUTCOMES:
            continue
        if await is_acknowledged(db, row_repo, issue_number, target_entity, session_name):
            continue
        return row_repo, issue_number
    return None


async def _record(
    db: BackboneDB | None,
    *,
    claim_id: int | None,
    repo: str,
    issue_number: int | None,
    target_entity: str | None,
    session_name: str,
    outcome: DeliveryOutcome,
    source: str,
    kind: str,
    preview: str,
) -> None:
    if db is None:
        return
    try:
        if claim_id is not None:
            await db.deliveries.finalize(claim_id, outcome.value)
            return
        await db.deliveries.record(
            issue_number=issue_number,
            target_entity=target_entity or session_name,
            session_name=session_name,
            outcome=outcome.value,
            source=source,
            repo=repo,
            kind=kind,
            preview=preview,
        )
    except Exception:
        log.exception("Failed to record delivery (non-fatal)")


async def _enqueue(
    db: BackboneDB | None,
    *,
    session_name: str,
    message: str,
    repo: str,
    issue_number: int | None,
    target_entity: str | None,
    source: str,
    kind: str,
    sender: str,
    source_key: str | None,
) -> str | None:
    """Store the message; say what happened (``stored`` / ``already_queued`` /
    ``failed``), or None when there is nothing to store it in."""
    if db is None:
        return None
    if kind == "issue" and (issue_number is None or target_entity is None):
        return None
    try:
        result = await db.queue.enqueue(
            session_name=session_name,
            message=message,
            issue_number=issue_number,
            target_entity=target_entity,
            delivery_kind=kind,
            source=source,
            repo=repo,
            sender=sender,
            source_key=source_key,
        )
    except Exception:
        log.exception("Could not store a %s for %s — the sender is told", kind, session_name)
        return "failed"
    if result.status == "inserted":
        log.info("Queued %s for %s (%s) via %s", kind, session_name, repo or "-", source or "?")
        return "stored"
    log.info("Same %s for %s already queued (from %s)", kind, session_name, sender or "?")
    return "already_queued"


async def safe_deliver(
    session_name: str,
    message: str,
    config: BackboneConfig,
    *,
    db: BackboneDB | None = None,
    repo: str = "",
    issue_number: int | None = None,
    target_entity: str | None = None,
    source: str = "",
    priority: bool = False,
    idle_since: float | None = None,
    enforce_issue_queue: bool = False,
    queue_scope: Collection[tuple[str, int]] | None = None,
    delivery_kind: str = "issue",
    sender: str = "",
    source_key: str | None = None,
    requeue: bool = True,
) -> DeliveryOutcome:
    """``deliver`` for callers that only act on the outcome."""
    report = await deliver(
        session_name,
        message,
        config,
        db=db,
        repo=repo,
        issue_number=issue_number,
        target_entity=target_entity,
        source=source,
        priority=priority,
        idle_since=idle_since,
        enforce_issue_queue=enforce_issue_queue,
        queue_scope=queue_scope,
        delivery_kind=delivery_kind,
        sender=sender,
        source_key=source_key,
        requeue=requeue,
    )
    return report.outcome


@_serialized
async def deliver(
    session_name: str,
    message: str,
    config: BackboneConfig,
    *,
    db: BackboneDB | None = None,
    repo: str = "",
    issue_number: int | None = None,
    target_entity: str | None = None,
    source: str = "",
    priority: bool = False,
    idle_since: float | None = None,
    enforce_issue_queue: bool = False,
    queue_scope: Collection[tuple[str, int]] | None = None,
    delivery_kind: str = "issue",
    sender: str = "",
    source_key: str | None = None,
    requeue: bool = True,
) -> DeliveryReport:
    """Deliver ``message`` to ``session_name`` if the agent can take it, else queue it.

    ``source`` names the code path for the delivery record (``issue-dispatcher``,
    ``api-messages``, …). ``sender`` is who is speaking (``from_entity``) and
    ``source_key`` the identity of the originating event when there is one;
    together they decide what counts as *the same* queued message.
    A queue drain sets ``requeue=False``: its existing leased row
    already holds the message, even when the displayed text gains an age note.
    """
    kind = delivery_kind
    trackable_issue = db is not None and issue_number is not None and target_entity is not None
    preview = message[:200]

    # 1. Issue queue gate
    if kind == "issue" and trackable_issue:
        if await _has_successful_issue_delivery(db, repo, issue_number, session_name):
            log.info(
                "Suppressed duplicate issue delivery %s#%s -> %s", repo, issue_number, session_name
            )
            return DeliveryReport(DeliveryOutcome.ALREADY_DELIVERED)
        if enforce_issue_queue:
            blocking = await _get_unacknowledged_gate_issue(
                db, session_name, repo, issue_number, queue_scope
            )
            if blocking is not None:
                log.info(
                    "Held %s#%s for %s pending acknowledgment of %s#%s",
                    repo,
                    issue_number,
                    session_name,
                    *blocking,
                )
                return DeliveryReport(DeliveryOutcome.AWAITING_ACK)

    # 2. Claim
    claim_id: int | None = None
    if kind == "issue" and trackable_issue:
        claim = await db.deliveries.claim(
            issue_number=issue_number,
            target_entity=target_entity,
            session_name=session_name,
            source=source,
            repo=repo,
            preview=preview,
        )
        if claim is None:
            return DeliveryReport(DeliveryOutcome.ALREADY_DELIVERED)
        claim_id = claim

    async def finish(outcome: DeliveryOutcome, *, queue: bool) -> DeliveryReport:
        stored: str | None = None
        if queue and requeue:
            stored = await _enqueue(
                db,
                session_name=session_name,
                message=message,
                repo=repo,
                issue_number=issue_number,
                target_entity=target_entity,
                source=source,
                kind=kind,
                sender=sender,
                source_key=source_key,
            )
        await _record(
            db,
            claim_id=claim_id,
            repo=repo,
            issue_number=issue_number,
            target_entity=target_entity,
            session_name=session_name,
            outcome=outcome,
            source=source,
            kind=kind,
            preview=preview,
        )
        return DeliveryReport(outcome, stored)

    # 3. Readiness
    profile = await get_session_intelligence(session_name, config, idle_since=idle_since)
    intel = profile.intelligence
    same_issue_comment = kind == "comment" and _comment_matches_active_issue(
        repo, issue_number, profile.current_repo, profile.current_issue
    )

    if kind == "plan_response":
        # A plan response is typed into the plan prompt itself, so it goes in
        # exactly when the agent is waiting for a plan decision — the one
        # condition every other kind must wait out — and never otherwise:
        # at an idle prompt a bare "2" would become a new instruction, and by
        # the time a queue drained the question would be gone. Never queued.
        if intel == SessionIntelligence.OFFLINE:
            return await finish(DeliveryOutcome.OFFLINE, queue=False)
        if not (intel == SessionIntelligence.WAITING_FOR_HUMAN and profile.reason == "plan"):
            return await finish(DeliveryOutcome.NOT_WAITING, queue=False)
        if await send_message(session_name, message, runtime_hint=profile.runtime):
            return await finish(DeliveryOutcome.DELIVERED, queue=False)
        return await finish(DeliveryOutcome.DELIVERY_FAILED, queue=False)

    if intel in BLOCKED_OUTCOMES:
        bypass = (priority and intel in _BYPASSABLE) or (
            same_issue_comment and intel in _ACTIVE_ISSUE_CONDITIONS
        )
        if not bypass:
            # Issue deliveries are re-attempted by the retry job; other kinds
            # are queued durably (except while merely settling / offline issues).
            queue = kind != "issue" or intel == SessionIntelligence.OFFLINE
            if intel == SessionIntelligence.SETTLING and kind == "issue":
                queue = False
            return await finish(DeliveryOutcome(intel.value), queue=queue)

    # 4. Paste + submit
    if await send_message(session_name, message, runtime_hint=profile.runtime):
        return await finish(DeliveryOutcome.DELIVERED, queue=False)
    return await finish(DeliveryOutcome.DELIVERY_FAILED, queue=True)
