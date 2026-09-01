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

import logging
from collections.abc import Collection
from typing import TYPE_CHECKING

from agent_backbone.services.routing._intelligence import get_session_intelligence
from agent_backbone.services.routing.models import SessionIntelligence
from agent_backbone.services.terminal import send_message

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.database import BackboneDB

log = logging.getLogger(__name__)

_SUCCESS_OUTCOMES = ("delivered", "retried")

# Conditions that block delivery, in priority order, with the outcome they produce
# and whether ``priority`` may bypass them.
_BLOCKING: dict[SessionIntelligence, tuple[str, bool]] = {
    SessionIntelligence.OFFLINE: ("offline", False),
    SessionIntelligence.WAITING_FOR_HUMAN: ("waiting_for_human", False),
    SessionIntelligence.AGENT_WORKING: ("agent_working", False),
    SessionIntelligence.HUMAN_TYPING: ("human_typing", True),
    SessionIntelligence.SETTLING: ("settling", True),
}


def outcome_queues(outcome: str, kind: str) -> bool:
    """Whether ``safe_deliver`` queued the message for this blocked outcome.

    Mirrors the queueing decision in ``safe_deliver``: non-issue kinds are
    queued durably on every blocking condition and on paste failure; issue
    deliveries rely on the retry job except when the agent is offline.
    """
    if outcome == "delivery_failed":
        return True
    if outcome not in {o for o, _ in _BLOCKING.values()}:
        return False
    if kind == "issue":
        return outcome == "offline"
    return True


def _comment_matches_active_issue(
    repo: str, issue_number: int | None, current_repo: str | None, current_issue: int | None
) -> bool:
    if issue_number is None or current_issue is None or issue_number != current_issue:
        return False
    return not repo or not current_repo or repo.casefold() == current_repo.casefold()


async def _is_acknowledged_for_session(
    db: BackboneDB, repo: str, issue_number: int, target_entity: str, session_name: str
) -> bool:
    if await db.is_acknowledged(issue_number, target_entity, repo=repo):
        return True
    return session_name != target_entity and await db.is_acknowledged(
        issue_number, session_name, repo=repo
    )


async def _has_successful_issue_delivery(
    db: BackboneDB, repo: str, issue_number: int, session_name: str
) -> bool:
    rows = await db.query_deliveries(
        issue_number=issue_number, session_name=session_name, limit=25, repo=repo, kind="issue"
    )
    return any((row.get("outcome") or "") in _SUCCESS_OUTCOMES for row in rows)


async def _get_unacknowledged_gate_issue(
    db: BackboneDB,
    session_name: str,
    repo: str,
    current_issue: int,
    queue_scope: Collection[tuple[str, int]] | None = None,
) -> tuple[str, int] | None:
    """The most recent successfully delivered issue still awaiting acknowledgment."""
    scope = {(r.casefold(), n) for r, n in (queue_scope or ())}
    rows = await db.query_deliveries(session_name=session_name, limit=100, kind="issue")
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
        if (row.get("outcome") or "") not in _SUCCESS_OUTCOMES:
            continue
        if await _is_acknowledged_for_session(
            db, row_repo, issue_number, target_entity, session_name
        ):
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
    outcome: str,
    flow_name: str,
    kind: str,
    preview: str,
) -> None:
    if db is None:
        return
    try:
        if claim_id is not None:
            await db.finalize_delivery_attempt(claim_id, outcome)
            return
        await db.record_delivery(
            issue_number,
            target_entity or session_name,
            session_name,
            outcome,
            flow_name,
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
    flow_name: str,
    kind: str,
) -> None:
    if db is None:
        return
    if kind == "issue" and (issue_number is None or target_entity is None):
        return
    try:
        await db.enqueue_message(
            session_name=session_name,
            message=message,
            issue_number=issue_number,
            target_entity=target_entity,
            delivery_kind=kind,
            flow_name=flow_name,
            repo=repo,
        )
        log.info("Queued %s for %s (%s) via %s", kind, session_name, repo or "-", flow_name or "?")
    except Exception:
        log.warning("Failed to enqueue message for %s (non-fatal)", session_name)


async def safe_deliver(
    session_name: str,
    message: str,
    config: BackboneConfig,
    *,
    db: BackboneDB | None = None,
    repo: str = "",
    issue_number: int | None = None,
    target_entity: str | None = None,
    flow_name: str = "",
    priority: bool = False,
    idle_since: float | None = None,
    enforce_issue_queue: bool = False,
    queue_scope: Collection[tuple[str, int]] | None = None,
    delivery_kind: str = "issue",
) -> str:
    """Deliver ``message`` to ``session_name`` if the agent can take it, else queue it.

    Returns one of: ``delivered``, ``offline``, ``waiting_for_human``,
    ``agent_working``, ``human_typing``, ``settling``, ``delivery_failed``,
    ``already_delivered``, ``awaiting_ack``.
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
            return "already_delivered"
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
                return "awaiting_ack"

    # 2. Claim
    claim_id: int | None = None
    if kind == "issue" and trackable_issue:
        claim = await db.claim_delivery_attempt(
            issue_number=issue_number,
            target_entity=target_entity,
            session_name=session_name,
            flow_name=flow_name,
            repo=repo,
            preview=preview,
        )
        if claim is None:
            return "already_delivered"
        claim_id = claim

    async def finish(outcome: str, *, queue: bool) -> str:
        if queue:
            await _enqueue(
                db,
                session_name=session_name,
                message=message,
                repo=repo,
                issue_number=issue_number,
                target_entity=target_entity,
                flow_name=flow_name,
                kind=kind,
            )
        await _record(
            db,
            claim_id=claim_id,
            repo=repo,
            issue_number=issue_number,
            target_entity=target_entity,
            session_name=session_name,
            outcome=outcome,
            flow_name=flow_name,
            kind=kind,
            preview=preview,
        )
        return outcome

    # 3. Readiness
    profile = await get_session_intelligence(session_name, config, idle_since=idle_since)
    intel = profile.intelligence
    same_issue_comment = kind == "comment" and _comment_matches_active_issue(
        repo, issue_number, profile.current_repo, profile.current_issue
    )

    if intel in _BLOCKING:
        outcome, bypassable = _BLOCKING[intel]
        bypass = (priority and bypassable) or (
            same_issue_comment
            and intel in (SessionIntelligence.AGENT_WORKING, SessionIntelligence.WAITING_FOR_HUMAN)
        )
        if not bypass:
            # Issue deliveries are re-attempted by the retry job; other kinds
            # are queued durably (except while merely settling / offline issues).
            queue = kind != "issue" or intel == SessionIntelligence.OFFLINE
            if intel == SessionIntelligence.SETTLING and kind == "issue":
                queue = False
            return await finish(outcome, queue=queue)

    # 4. Paste + submit
    if await send_message(session_name, message, runtime_hint=profile.runtime):
        return await finish("delivered", queue=False)
    return await finish("delivery_failed", queue=True)
