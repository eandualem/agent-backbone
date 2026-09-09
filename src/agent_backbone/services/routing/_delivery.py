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
import uuid
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from functools import wraps
from typing import TYPE_CHECKING, TypeVar
from weakref import WeakValueDictionary

from agent_backbone.models import BLOCKED_OUTCOMES, SUCCESS_OUTCOMES, DeliveryOutcome
from agent_backbone.services.agents import note_submission
from agent_backbone.services.routing._intelligence import get_session_intelligence
from agent_backbone.services.routing.models import SessionIntelligence, SessionProfile
from agent_backbone.services.runtimes import SubmissionUnconfirmed, send_message

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.database import BackboneDB

log = logging.getLogger(__name__)

_BYPASSABLE = frozenset({SessionIntelligence.HUMAN_TYPING, SessionIntelligence.SETTLING})
"""Blocking conditions ``priority`` may push through. Busy and waiting never are."""


@dataclass(frozen=True)
class DeliveryReport:
    """What ``safe_deliver`` did: the outcome, and — when it could not deliver —
    whether the message is now in the queue.

    ``queue`` is ``stored`` (a new row), ``already_queued`` (the same message
    from this sender was already waiting; nothing added), ``failed`` (the
    database refused it — the message is NOT held anywhere) or None (nothing
    needed queueing: delivered, or a kind that is never queued).
    """

    outcome: DeliveryOutcome
    queue: str | None = None
    unconfirmed: bool = False
    """A duplicate claim can still be in flight; it is not a delivery receipt."""
    operation_id: str | None = None
    delivery_id: int | None = None
    queue_id: int | None = None

    @property
    def queued(self) -> bool:
        """True only when a row for this message exists in the queue."""
        return self.queue in ("stored", "already_queued")


@dataclass(frozen=True)
class _QueueReceipt:
    status: str | None = None
    id: int | None = None
    operation_id: str | None = None
    error_type: str | None = None


_session_locks: WeakValueDictionary[tuple[int, str], asyncio.Lock] = WeakValueDictionary()


_Result = TypeVar("_Result")


def _serialized(fn: Callable[..., Awaitable[_Result]]):
    """One gate/paste/record transaction per session; idle locks are released.

    Each caller holds its lock strongly while waiting or delivering. The weak
    cache keeps unrelated sessions concurrent without retaining forgotten names.
    """

    @wraps(fn)
    async def locked(session_name: str, *args, **kwargs):
        key = (id(asyncio.get_running_loop()), session_name)
        lock = _session_locks.setdefault(key, asyncio.Lock())
        async with lock:
            db = kwargs.get("db")
            source_key = kwargs.get("source_key") or ""
            if db is not None and source_key.startswith("review-start:"):
                async with db.events.review_delivery(source_key) as eligible:
                    if not eligible:
                        return DeliveryReport(DeliveryOutcome.ALREADY_DELIVERED)
                    return await fn(session_name, *args, **kwargs)
            return await fn(session_name, *args, **kwargs)

    return locked


def queue_detail(report: DeliveryReport, session_name: str, expiry_minutes: int) -> str:
    """One plain sentence about what happened, for people and agents alike."""
    if report.unconfirmed and report.queue_id is None:
        return "Submission is uncertain and was not retained; inspect the terminal before retrying."
    if report.unconfirmed and report.queue_id is not None:
        return (
            f"Submission to {session_name} is uncertain; message {report.queue_id} is held "
            "without automatic retry. Inspect the transcript and use backbone inbox to resolve it."
        )
    if report.outcome == DeliveryOutcome.DELIVERED:
        return f"Delivered to {session_name}."
    why = report.outcome.value.replace("_", " ")
    if report.queue == "stored":
        return (
            f"Queued: {session_name} is {why}; the message is stored and will be delivered "
            f"when the agent is ready (normally expires after {expiry_minutes} minutes; "
            "active swarm coordination is retained)."
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
    operation_id: str,
) -> int | None:
    if db is None:
        return None
    try:
        if claim_id is not None:
            await db.deliveries.finalize(claim_id, outcome.value, operation_id=operation_id)
            return claim_id
        return await db.deliveries.record(
            issue_number=issue_number,
            target_entity=target_entity or session_name,
            session_name=session_name,
            outcome=outcome.value,
            source=source,
            repo=repo,
            kind=kind,
            preview=preview,
            operation_id=operation_id,
        )
    except Exception as exc:
        log.error("Failed to record delivery (non-fatal; %s)", type(exc).__name__)
        return None


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
    operation_id: str,
    uncertain: bool = False,
) -> _QueueReceipt:
    """Store the message; say what happened (``stored`` / ``already_queued`` /
    ``failed``), or None when there is nothing to store it in."""
    if db is None:
        return _QueueReceipt()
    if kind == "issue" and (issue_number is None or target_entity is None):
        return _QueueReceipt()
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
            operation_id=operation_id,
            **({"uncertain": True} if uncertain else {}),
        )
    except Exception as exc:
        log.error(
            "Could not store a %s for %s — the sender is told (%s)",
            kind,
            session_name,
            type(exc).__name__,
        )
        return _QueueReceipt("failed", error_type=type(exc).__name__)
    queue_id = result.id if isinstance(result.id, int) else None
    stored_operation = result.operation_id if isinstance(result.operation_id, str) else operation_id
    if result.status == "inserted":
        log.info("Queued %s for %s (%s) via %s", kind, session_name, repo or "-", source or "?")
        return _QueueReceipt("stored", queue_id, stored_operation)
    log.info("Same %s for %s already queued (from %s)", kind, session_name, sender or "?")
    return _QueueReceipt("already_queued", queue_id, stored_operation)


@_serialized
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
    operation_id: str | None = None,
    queue_id: int | None = None,
    event_id: int | None = None,
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
    operation_id = operation_id or uuid.uuid4().hex
    trackable_issue = db is not None and issue_number is not None and target_entity is not None
    preview = message[:200]

    if db is not None and requeue:
        held = await db.queue.held_receipt(
            session_name,
            message,
            sender,
            source_key,
            repo,
            issue_number,
            kind,
        )
        if held is not None:
            return DeliveryReport(
                DeliveryOutcome.AWAITING_ACK,
                "already_queued",
                held["status"] == "uncertain",
                held["operation_id"],
                queue_id=held["id"],
            )

    # 1. Issue queue gate
    if kind == "issue" and trackable_issue:
        if await _has_successful_issue_delivery(db, repo, issue_number, session_name):
            log.info(
                "Suppressed duplicate issue delivery %s#%s -> %s", repo, issue_number, session_name
            )
            return DeliveryReport(DeliveryOutcome.ALREADY_DELIVERED, operation_id=operation_id)
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
                return DeliveryReport(DeliveryOutcome.AWAITING_ACK, operation_id=operation_id)

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
            operation_id=operation_id,
        )
        if claim is None:
            return DeliveryReport(
                DeliveryOutcome.ALREADY_DELIVERED, unconfirmed=True, operation_id=operation_id
            )
        claim_id = claim

    profile: SessionProfile | None = None

    async def finish(outcome: DeliveryOutcome, *, queue: bool) -> DeliveryReport:
        receipt = _QueueReceipt()
        if queue and requeue:
            receipt = await _enqueue(
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
                operation_id=operation_id,
                uncertain=uncertain,
            )
        elif uncertain and db is not None and queue_id is not None:
            await db.queue.hold_uncertain(queue_id)
        trace = receipt.operation_id or operation_id
        stored_queue_id = receipt.id if receipt.id is not None else queue_id
        delivery_id = await _record(
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
            operation_id=trace,
        )
        if db is not None:
            details = {
                "delivery_kind": kind,
                "priority": priority,
                "requeue": requeue,
                "queue_status": receipt.status,
            }
            if profile is not None:
                reason = profile.reason
                if reason is not None and reason not in {
                    "plan",
                    "permission",
                    "question",
                    "quota",
                    "provider",
                }:
                    reason = "unknown"
                details.update(
                    condition=profile.intelligence.value,
                    state=str(profile.agent_state),
                    state_source=profile.state_source,
                    reason=reason,
                )
            metadata = {
                "operation_id": trace,
                "agent_name": session_name,
                "source": source,
                "runtime": profile.runtime if profile is not None else "",
                "repo": repo,
                "issue_number": issue_number,
                "delivery_id": delivery_id,
                "queue_id": stored_queue_id,
                "event_id": event_id,
            }
            if outcome == DeliveryOutcome.DELIVERED:
                code, severity = "submitted", "info"
            elif outcome == DeliveryOutcome.DELIVERY_FAILED:
                code, severity = "submission_unconfirmed", "error"
            else:
                code, severity = f"deferred_{outcome.value}", "info"
                if profile is not None and profile.reason in {"quota", "provider"}:
                    code += f"_{profile.reason}"
            await db.diagnostics.record(
                category="delivery", code=code, severity=severity, details=details, **metadata
            )
            if receipt.status is not None:
                await db.diagnostics.record(
                    category="queue",
                    code="queue_storage_failed" if receipt.status == "failed" else "queue_stored",
                    severity="error" if receipt.status == "failed" else "info",
                    details={**details, "error_type": receipt.error_type},
                    **metadata,
                )
        return DeliveryReport(
            outcome,
            receipt.status,
            operation_id=trace,
            delivery_id=delivery_id,
            queue_id=stored_queue_id,
        )

    async def record_exception(code: str, stage: str, exc: Exception) -> None:
        if db is not None:
            await db.diagnostics.record(
                category="delivery",
                code=code,
                severity="error",
                operation_id=operation_id,
                agent_name=session_name,
                source=source,
                runtime=profile.runtime if profile is not None else "",
                repo=repo,
                issue_number=issue_number,
                delivery_id=claim_id,
                queue_id=queue_id,
                event_id=event_id,
                details={"stage": stage, "error_type": type(exc).__name__, "delivery_kind": kind},
            )

    uncertain = False

    async def submit() -> bool:
        nonlocal uncertain
        note_submission(config.state_dir, session_name)
        try:
            return await send_message(session_name, message, runtime_hint=profile.runtime)
        except SubmissionUnconfirmed as exc:
            uncertain = True
            await record_exception("submission_unconfirmed", "submission", exc)
            return False
        except Exception as exc:
            await record_exception("submission_unconfirmed", "submission", exc)
            raise

    # 3. Readiness
    try:
        profile = await get_session_intelligence(session_name, config, idle_since=idle_since)
    except Exception as exc:
        await record_exception("readiness_failed", "readiness", exc)
        raise
    intel = profile.intelligence

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
        if await submit():
            return await finish(DeliveryOutcome.DELIVERED, queue=False)
        return await finish(DeliveryOutcome.DELIVERY_FAILED, queue=False)

    if intel in BLOCKED_OUTCOMES:
        bypass = priority and intel in _BYPASSABLE
        if not bypass:
            # Issue deliveries are re-attempted by the retry job; other kinds
            # are queued durably (except while merely settling / offline issues).
            queue = kind != "issue" or intel == SessionIntelligence.OFFLINE
            if intel == SessionIntelligence.SETTLING and kind == "issue":
                queue = False
            return await finish(DeliveryOutcome(intel.value), queue=queue)

    if db is not None and await db.queue.has_uncertain(session_name):
        # A previous paste may still occupy the input box. Hold new messages
        # too; the cooperative inbox is the safe way to resolve that ambiguity.
        return await finish(DeliveryOutcome.AWAITING_ACK, queue=True)

    # 4. Paste + submit
    if await submit():
        return await finish(DeliveryOutcome.DELIVERED, queue=False)
    report = await finish(DeliveryOutcome.DELIVERY_FAILED, queue=True)
    if uncertain:
        return DeliveryReport(
            report.outcome,
            report.queue,
            True,
            report.operation_id,
            report.delivery_id,
            report.queue_id,
        )
    return report


@_serialized
async def checkpoint_inbox(
    session_name: str, *, db: BackboneDB, acknowledge: list[str] | None = None
) -> dict:
    """Read/ack checkpoints under the same session lock as terminal delivery."""
    if acknowledge:
        return {"acknowledged": await db.queue.acknowledge_checkpoint(session_name, acknowledge)}
    return {"session": session_name, "messages": await db.queue.checkpoint(session_name)}
