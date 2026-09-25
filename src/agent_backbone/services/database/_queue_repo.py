"""The message queue — deferred deliveries, leased in batches."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import bindparam, text

from agent_backbone.models import BRIEF_SOURCE, RETIREMENT_REASONS, SUBSCRIPTION_KIND
from agent_backbone.services.database._diagnostics_repo import DiagnosticRepo
from agent_backbone.services.database._repo import Repo
from agent_backbone.services.database._time import cutoff_iso, now_iso

_MAX_ENQUEUE_ATTEMPTS = 3
"""Retry a duplicate that completes between conflict detection and receipt lookup."""

_INSERT_COLUMNS = """(operation_id, session_name, message, repo, issue_number, target_entity,
                delivery_kind, source, enqueued_at, status, sender, dedup_key, priority)
               VALUES (:operation_id, :session_name, :message, :repo, :issue_number, :target_entity,
                       :delivery_kind, :source, :enqueued_at, :initial_status,
                       :sender, :dedup_key, :priority)"""

Revise = Callable[[str | None], str]
"""Builds a notice's text from its current text (None when there is none yet)."""
Notices = Callable[[list[dict]], list[tuple[str, str, str, Revise]]]
"""Maps expired rows to ``(session, source, target_entity, revise)`` notices."""

SUBSCRIPTION_BATCH_LIMIT = 25
"""Lines one subscription batch lists; further lines open the next batch."""


@dataclass(frozen=True)
class EnqueueResult:
    """What ``enqueue`` did: ``inserted`` (a new row), ``already_queued`` (the
    same message is already waiting — nothing added) or, for a subscription
    batch, ``appended`` (the open batch grew). All return the stored row's id
    and operation identity.
    A database error is raised, never swallowed: the caller decides what to
    tell the sender."""

    status: str
    id: int | None = None
    operation_id: str | None = None
    context_offers: tuple[tuple[int, str], ...] = ()
    """Exact immutable high-batch rows, each offered under its own receipt ID."""

    @property
    def stored(self) -> bool:
        """Whether a row for this message now exists in the queue."""
        return self.status in ("inserted", "already_queued", "appended")


def dedup_key_for(message: str, sender: str, source_key: str | None) -> str:
    """The identity of a non-issue message for duplicate detection.

    The source event (a GitHub comment id, an issue lifecycle event) when
    the caller has one; otherwise the sender and the text together, so the
    same words from two senders are two messages and one sender repeating
    itself by accident is one.
    """
    if source_key:
        return f"src:{source_key}"
    digest = hashlib.sha256(f"{sender}\x00{message}".encode()).hexdigest()
    return f"msg:{digest}"


def _fill_batch(message: str, lines: list[str]) -> tuple[str, list[str]]:
    """``message`` (a header, then listed lines) with as many of ``lines`` as fit
    under ``SUBSCRIPTION_BATCH_LIMIT``, and the lines that did not."""
    parts = message.split("\n")
    room = max(0, SUBSCRIPTION_BATCH_LIMIT - (len(parts) - 1))
    return "\n".join(parts + lines[:room]), lines[room:]


class QueueRepo(Repo):
    async def prune(self, retention_days: int = 30) -> int:
        """Delete completed queue bodies after retention, measured from completion.

        Pending and leased messages are never deleted; expiry is a separate
        operation that records their outcome. Legacy rows without a completion
        timestamp are retained because their age cannot be established.
        """
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    "DELETE FROM message_queue WHERE status IN ('delivered', 'expired')"
                    " AND delivered_at < :cutoff"
                ),
                {"cutoff": cutoff_iso(days=retention_days)},
            )
            return result.rowcount

    async def enqueue(
        self,
        *,
        session_name: str,
        message: str,
        issue_number: int | None = None,
        target_entity: str | None = None,
        delivery_kind: str = "issue",
        source: str = "",
        repo: str = "",
        sender: str = "",
        source_key: str | None = None,
        operation_id: str | None = None,
        uncertain: bool = False,
        priority: int = 0,
    ) -> EnqueueResult:
        """Store a message for later delivery.

        ``sender`` and ``source_key`` form the duplicate key for non-issue
        kinds (``dedup_key_for``); issue notifications are unique per
        ``(session, repo, issue)`` while pending.
        """
        async with self._tx() as conn:
            params = {
                "operation_id": operation_id or uuid.uuid4().hex,
                "session_name": session_name,
                "message": message,
                "repo": repo,
                "issue_number": issue_number,
                "target_entity": target_entity,
                "delivery_kind": delivery_kind,
                "source": source,
                "enqueued_at": now_iso(),
                "initial_status": "uncertain" if uncertain else "pending",
                "sender": sender,
                "dedup_key": dedup_key_for(message, sender, source_key),
                "priority": priority,
            }

            if delivery_kind == "issue" and issue_number is not None:
                conflict = """ON CONFLICT (session_name, repo, issue_number)
                       WHERE delivery_kind = 'issue'
                         AND status IN ('pending','in_progress','checkpoint','uncertain')
                         AND issue_number IS NOT NULL
                       DO NOTHING"""
            elif delivery_kind == "issue":
                conflict = ""
            else:
                conflict = """ON CONFLICT (session_name, dedup_key)
                       WHERE delivery_kind != 'issue'
                         AND status IN ('pending','in_progress','checkpoint','uncertain')
                       DO NOTHING"""

            sql = f"INSERT INTO message_queue {_INSERT_COLUMNS} {conflict} RETURNING id"
            for _attempt in range(_MAX_ENQUEUE_ATTEMPTS):
                result = await conn.execute(text(sql), params)
                row = result.fetchone()
                if row is not None:
                    return EnqueueResult("inserted", row._mapping["id"], params["operation_id"])

                # Resolve the exact conflict key, including a row leased by a
                # drain. Never correlate using a message preview or its age.
                key = (
                    "session_name = :session_name AND repo = :repo "
                    "AND issue_number = :issue_number AND delivery_kind = 'issue'"
                    if delivery_kind == "issue"
                    else "session_name = :session_name AND dedup_key = :dedup_key "
                    "AND delivery_kind != 'issue'"
                )
                existing = await conn.execute(
                    text(
                        "UPDATE message_queue SET operation_id = "
                        "COALESCE(operation_id, :operation_id), "
                        "status = CASE WHEN :initial_status = 'uncertain' "
                        "THEN 'uncertain' ELSE status END "
                        f"WHERE {key} AND "
                        "status IN ('pending', 'in_progress', 'checkpoint', 'uncertain') "
                        "RETURNING id, operation_id"
                    ),
                    params,
                )
                stored = existing.mappings().one_or_none()
                if stored is not None:
                    return EnqueueResult("already_queued", stored["id"], stored["operation_id"])
                # Under READ COMMITTED the conflicting row can complete before
                # this statement's snapshot. Recheck insertion instead of
                # inventing a receipt for a row that is no longer waiting.
            raise RuntimeError("Queue changed repeatedly during enqueue; retry the message")

    async def _revise_in(
        self, conn, session_name: str, source: str, target_entity: str, revise: Revise
    ) -> None:
        """Rewrite the session's waiting direct message from ``source`` for
        ``target_entity`` as ``revise(its text)``, or insert ``revise(None)``:
        one notice per party that grows, not a stream. A notice already out for
        delivery is left alone and a new one opens behind it, with its own
        identity so identical text cannot fold into the one being delivered."""
        lock = " FOR UPDATE" if conn.dialect.name == "postgresql" else ""
        found = await conn.execute(
            text(
                "SELECT id, message FROM message_queue WHERE session_name = :session "
                "AND source = :source AND target_entity = :target "
                "AND status = 'pending' ORDER BY id DESC LIMIT 1" + lock
            ),
            {"session": session_name, "source": source, "target": target_entity},
        )
        row = found.mappings().first()
        if row is not None:
            updated = await conn.execute(
                text(
                    "UPDATE message_queue SET message = :message "
                    "WHERE id = :id AND status = 'pending' AND message = :previous"
                ),
                {"id": row["id"], "message": revise(row["message"]), "previous": row["message"]},
            )
            if updated.rowcount:
                return
        message = revise(None)
        await conn.execute(
            text(f"INSERT INTO message_queue {_INSERT_COLUMNS}"),
            {
                "operation_id": uuid.uuid4().hex,
                "session_name": session_name,
                "message": message,
                "repo": "",
                "issue_number": None,
                "target_entity": target_entity,
                "delivery_kind": "direct_message",
                "source": source,
                "enqueued_at": now_iso(),
                "initial_status": "pending",
                "sender": "",
                "dedup_key": dedup_key_for(message, "", uuid.uuid4().hex),
                "priority": 0,
            },
        )

    async def enqueue_subscription(
        self,
        *,
        session_name: str,
        header: str,
        lines: list[str],
        priority: int,
        source: str = "",
        operation_id: str | None = None,
    ) -> EnqueueResult:
        """Add subscribed events to the session's open normal batch, or open one.

        One pending normal batch per session: later events append to it so
        the agent reads one message, not a stream. A batch already leased for
        delivery is left alone and a new one opens behind it; a batch with
        ``SUBSCRIPTION_BATCH_LIMIT`` lines is full and the rest open the next,
        so every event keeps its id and link. A high batch is always its own
        row: its text may already be offered to a working agent as hook
        context, and an offer never changes under the hook's feet (the hook
        hands over every pending offer at once). Returns ``appended`` (only an
        existing row grew) or ``inserted`` (the last row opened).
        """
        async with self._tx() as conn:
            lock = " FOR UPDATE" if conn.dialect.name == "postgresql" else ""
            rest = list(lines)
            offers: list[tuple[int, str]] = []
            grown: EnqueueResult | None = None
            if not priority:
                existing = await conn.execute(
                    text(
                        "SELECT id, operation_id, message FROM message_queue "
                        "WHERE session_name = :session AND delivery_kind = :kind "
                        "AND priority = 0 AND status = 'pending' ORDER BY id DESC LIMIT 1" + lock
                    ),
                    {"session": session_name, "kind": SUBSCRIPTION_KIND},
                )
                row = existing.mappings().first()
                if row is not None:
                    message, rest = _fill_batch(row["message"], rest)
                    if message != row["message"]:
                        result = await conn.execute(
                            text(
                                "UPDATE message_queue SET message = :message "
                                "WHERE id = :id AND status = 'pending'"
                            ),
                            {"id": row["id"], "message": message},
                        )
                        if result.rowcount:
                            grown = EnqueueResult("appended", row["id"], row["operation_id"])
                        else:
                            rest = list(lines)  # leased meanwhile: open a new batch
            if grown is not None and not rest:
                return grown
            while True:
                message, rest = _fill_batch(header, rest)
                params = {
                    "operation_id": operation_id or uuid.uuid4().hex,
                    "session_name": session_name,
                    "message": message,
                    "repo": "",
                    "issue_number": None,
                    "target_entity": session_name,
                    "delivery_kind": SUBSCRIPTION_KIND,
                    "source": source,
                    "enqueued_at": now_iso(),
                    "initial_status": "pending",
                    "sender": "",
                    "dedup_key": f"sub:{uuid.uuid4().hex}",
                    "priority": priority,
                }
                result = await conn.execute(
                    text(f"INSERT INTO message_queue {_INSERT_COLUMNS} RETURNING id"), params
                )
                row_id = result.scalar_one()
                if priority:
                    offers.append((row_id, message))
                if not rest:
                    return EnqueueResult("inserted", row_id, params["operation_id"], tuple(offers))
                operation_id = None

    async def pending_count(self, session_name: str) -> int:
        """How many messages are waiting for one session."""
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    "SELECT COUNT(*) FROM message_queue "
                    "WHERE session_name = :session AND status = 'pending'"
                ),
                {"session": session_name},
            )
            return int(result.scalar_one())

    async def sessions_with_pending(self) -> list[str]:
        async with self._tx() as conn:
            result = await conn.execute(
                text("SELECT DISTINCT session_name FROM message_queue WHERE status = 'pending'")
            )
            return [row._mapping["session_name"] for row in result.fetchall()]

    async def dequeue(self, session_name: str, limit: int = 10) -> list[dict]:
        """Atomically claim pending messages for a session: its startup brief
        first, then high-priority subscription batches, then oldest first."""
        async with self._tx() as conn:
            now = now_iso()
            lock = "FOR UPDATE SKIP LOCKED" if conn.dialect.name == "postgresql" else ""
            sql = f"""UPDATE message_queue SET status='in_progress', leased_at=:now
                     WHERE id IN (
                         SELECT id FROM message_queue
                         WHERE session_name=:session AND status='pending'
                         ORDER BY source = :brief DESC, priority DESC, enqueued_at ASC
                         LIMIT :lim
                         {lock}
                     ) RETURNING *"""
            result = await conn.execute(
                text(sql),
                {"session": session_name, "lim": limit, "now": now, "brief": BRIEF_SOURCE},
            )
            rows = [dict(row._mapping) for row in result.fetchall()]
            for row in rows:
                await self._ensure_operation_id(conn, row)
            rows.sort(
                key=lambda row: (
                    row.get("source") != BRIEF_SOURCE,
                    -(row.get("priority") or 0),
                    row["enqueued_at"],
                )
            )
            return rows

    async def release(self, message_id: int) -> None:
        async with self._tx() as conn:
            await conn.execute(
                text(
                    """UPDATE message_queue SET status='pending', leased_at=NULL
                       WHERE id = :id AND status = 'in_progress'"""
                ),
                {"id": message_id},
            )

    async def expire_stale_leases(self, max_age_minutes: int = 5) -> int:
        """Return leases older than the cutoff to ``pending`` (the deliverer died mid-batch)."""
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    """UPDATE message_queue SET status='pending', leased_at=NULL
                       WHERE status = 'in_progress' AND leased_at < :cutoff"""
                ),
                {"cutoff": cutoff_iso(minutes=max_age_minutes)},
            )
            return result.rowcount

    async def mark_delivered(self, message_id: int, *, reason: str | None = None) -> None:
        """Complete the lease; ``reason`` identifies intentional retirement."""
        if reason is not None and reason not in RETIREMENT_REASONS | {"already_delivered"}:
            raise ValueError("Invalid queue retirement reason")
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    """UPDATE message_queue
                       SET status = 'delivered', delivered_at = :delivered_at
                       WHERE id = :id AND status = 'in_progress' RETURNING *"""
                ),
                {"delivered_at": now_iso(), "id": message_id},
            )
            row = result.mappings().first()
            if row is not None:
                row = dict(row)
                await self._ensure_operation_id(conn, row)
        if row is not None and reason is not None:
            await self._record_lifecycle(row, f"retired_{reason}", reason=reason)

    async def _ensure_operation_id(self, conn, row: dict) -> None:
        """Give legacy rows one identity, derived only from their exact database key."""
        if not row.get("operation_id"):
            row["operation_id"] = uuid.uuid5(uuid.NAMESPACE_URL, f"backbone:queue:{row['id']}").hex
            await conn.execute(
                text("UPDATE message_queue SET operation_id = :operation WHERE id = :id"),
                {"operation": row["operation_id"], "id": row["id"]},
            )

    async def _record_lifecycle(
        self, row, code: str, *, reason: str | None = None, delivery_id: int | None = None
    ) -> None:
        """Persist metadata after completion; diagnostics cannot roll back the queue."""
        await DiagnosticRepo(self._engine).record(
            category="queue",
            code=code,
            operation_id=row["operation_id"]
            or uuid.uuid5(uuid.NAMESPACE_URL, f"backbone:queue:{row['id']}").hex,
            severity="warning" if code == "queue_expired" else "info",
            agent_name=row["session_name"],
            source=row["source"],
            repo=row["repo"],
            issue_number=row["issue_number"],
            queue_id=row["id"],
            delivery_id=delivery_id,
            details={
                "delivery_kind": row["delivery_kind"],
                "queue_status": row["status"],
                "reason": reason,
            },
        )

    async def expire_pending(
        self,
        max_age_minutes: int = 30,
        *,
        protected_sessions: tuple[str, ...] = (),
        notices: Notices | None = None,
    ) -> list[dict]:
        """Expire pending messages older than the cutoff and, in the same
        transaction, leave a delivery row with outcome ``expired`` for each,
        so a dropped message is never lost from the record. Returns the rows.

        Leased rows are not considered: ``expire_stale_leases`` returns them to
        ``pending`` long before this cutoff, so they expire on the next sweep.
        Subscription batches never expire: they are facts, not conversation.
        Nor does a restart's continuation message (source ``agent-restart``):
        it belongs to the transition and waits for its replacement session.
        Nor does an expiry notice (source ``queue-expiry``): it reports a loss.
        Nor does anything queued for a session with an ``uncertain`` row: that
        row holds the whole queue until it is acknowledged, so the wait
        measures the hold, not whether the message is still wanted. After the
        hold is acknowledged the queue gets a full window from that moment.

        ``notices`` turns the expired rows into notices written in the same
        transaction: an expiry is never committed without them, and a failure
        rolls both back for the next sweep.
        """
        async with self._tx() as conn:
            now = now_iso()
            result = await conn.execute(
                text(
                    """UPDATE message_queue SET status = 'expired', delivered_at = :now
                       WHERE status = 'pending' AND enqueued_at < :cutoff
                         AND delivery_kind != 'subscription'
                         AND source NOT IN ('agent-restart', 'queue-expiry')
                         AND session_name NOT IN (
                             SELECT session_name FROM message_queue WHERE status = 'uncertain')
                         AND session_name NOT IN (
                             SELECT session_name FROM deliveries
                             WHERE source = 'uncertain-acknowledged' AND created_at >= :cutoff)
                         AND session_name NOT IN :protected
                         AND COALESCE(sender, '') NOT IN :protected
                       RETURNING *"""
                ).bindparams(bindparam("protected", expanding=True)),
                {
                    "now": now,
                    "cutoff": cutoff_iso(minutes=max_age_minutes),
                    "protected": list(protected_sessions),
                },
            )
            rows = [dict(row._mapping) for row in result.fetchall()]
            for row in rows:
                await self._ensure_operation_id(conn, row)
                message = row.get("message") or ""
                delivery = await conn.execute(
                    text(
                        """INSERT INTO deliveries
                           (operation_id, kind, repo, issue_number, target_entity, session_name,
                            outcome, source, preview, created_at)
                           VALUES (:operation_id, :kind, :repo, :issue_number, :target_entity,
                                   :session_name, 'expired', :source, :preview, :created_at)
                           RETURNING id"""
                    ),
                    {
                        "operation_id": row["operation_id"],
                        "kind": row.get("delivery_kind") or "issue",
                        "repo": row.get("repo") or "",
                        "issue_number": row.get("issue_number"),
                        "target_entity": row.get("target_entity") or row.get("session_name") or "",
                        "session_name": row.get("session_name") or "",
                        "source": row.get("source") or "queue",
                        "preview": message[:120],
                        "created_at": now,
                    },
                )
                row["delivery_id"] = delivery.scalar_one()
            if notices is not None and rows:
                for session, source, target, revise in notices(rows):
                    await self._revise_in(conn, session, source, target, revise)
        for row in rows:
            await self._record_lifecycle(row, "queue_expired", delivery_id=row["delivery_id"])
        return rows

    async def purge_for_issue(self, issue_number: int, *, repo: str = "") -> int:
        """Mark pending/leased messages for an issue as delivered (issue closed)."""
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    """UPDATE message_queue
                       SET status = 'delivered', delivered_at = :delivered_at
                       WHERE repo = :repo AND issue_number = :issue_number
                         AND
                         status IN ('pending', 'in_progress', 'checkpoint', 'uncertain')
                       RETURNING *"""
                ),
                {"delivered_at": now_iso(), "repo": repo, "issue_number": issue_number},
            )
            rows = [dict(row) for row in result.mappings()]
            for row in rows:
                await self._ensure_operation_id(conn, row)
        for row in rows:
            await self._record_lifecycle(row, "retired_issue_closed", reason="issue_closed")
        return len(rows)

    async def hold_uncertain(self, message_id: int) -> None:
        """An attempted paste may have been accepted. Do not retry it automatically."""
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "UPDATE message_queue SET status='uncertain', leased_at=NULL "
                    "WHERE id=:id AND status IN ('pending','in_progress')"
                ),
                {"id": message_id},
            )

    async def checkpoint(self, session_name: str, limit: int = 10) -> list[dict]:
        """Take direct messages out of terminal delivery until explicit acknowledgement.

        Re-reading returns the same unacknowledged IDs, including after a lost
        HTTP response or process restart. Ambiguous pastes are labeled uncertain.
        Issue notifications retain the issue routing/acknowledgement protocol.
        """
        async with self._tx() as conn:
            params = {"session": session_name, "limit": limit}
            held = await conn.execute(
                text(
                    "SELECT * FROM message_queue WHERE session_name=:session "
                    "AND status IN ('checkpoint','uncertain') ORDER BY enqueued_at,id LIMIT :limit"
                ),
                params,
            )
            rows = [dict(r) for r in held.mappings()]
            params["limit"] = max(0, limit - len(rows))
            lock = "FOR UPDATE SKIP LOCKED" if conn.dialect.name == "postgresql" else ""
            claimed = await conn.execute(
                text(
                    "UPDATE message_queue SET status='checkpoint', leased_at=NULL "
                    "WHERE status='pending' AND id IN (SELECT id FROM message_queue "
                    "WHERE session_name=:session AND status='pending' "
                    "AND delivery_kind='direct_message' ORDER BY enqueued_at,id LIMIT :limit "
                    + lock
                    + ") RETURNING *"
                ),
                params,
            )
            rows.extend(dict(r) for r in claimed.mappings())
            for row in rows:
                await self._ensure_operation_id(conn, row)
                row["ack_token"] = f"{row['id']}:{row['operation_id']}"
            return sorted(rows, key=lambda r: (r["enqueued_at"], r["id"]))

    async def acknowledge_checkpoint(self, session_name: str, tokens: list[str]) -> list[str]:
        """Acknowledge only this agent's checkpoint/uncertain rows; repeatable."""
        if not tokens:
            return []
        params = {"session": session_name, "now": now_iso()}
        predicates = []
        for index, token in enumerate(set(tokens)):
            try:
                row_id, operation = token.split(":", 1)
                params[f"id{index}"] = int(row_id)
                if not operation:
                    raise ValueError("Missing operation identity")
            except (ValueError, AttributeError) as exc:
                raise ValueError(
                    "Use ack_token from the inbox; numeric IDs are not receipts"
                ) from exc
            params[f"op{index}"] = operation
            predicates.append(f"(id=:id{index} AND operation_id=:op{index})")
        identity = "(" + " OR ".join(predicates) + ")"
        async with self._tx() as conn:
            records = await conn.execute(
                text(
                    "SELECT id,operation_id,status FROM message_queue WHERE session_name=:session "
                    "AND " + identity + " AND status IN ('checkpoint','uncertain','delivered')"
                ),
                params,
            )
            records = records.all()
            # A released uncertain row is recorded as such: the queue it held
            # gets a fresh expiry window from now (``expire_pending``).
            was_uncertain = {r[0] for r in records if r[2] == "uncertain"}
            if {f"{r[0]}:{r[1]}" for r in records} != set(tokens):
                raise ValueError("Receipts must match this agent's inbox; read inbox first")
            result = await conn.execute(
                text(
                    "UPDATE message_queue SET status='delivered', delivered_at=:now "
                    "WHERE session_name=:session AND "
                    + identity
                    + " AND status IN ('checkpoint','uncertain') RETURNING *"
                ),
                params,
            )
            rows = [dict(r) for r in result.mappings()]
            for row in rows:
                await self._ensure_operation_id(conn, row)
                await conn.execute(
                    text(
                        "INSERT INTO deliveries (operation_id,kind,repo,issue_number,target_entity,"
                        "session_name,outcome,source,preview,created_at) VALUES "
                        "(:op,:kind,:repo,:issue,:session,:session,'delivered',:source,:preview,:now)"
                    ),
                    {
                        "session": session_name,
                        "now": params["now"],
                        "op": row["operation_id"],
                        "kind": row["delivery_kind"],
                        "repo": row["repo"],
                        "issue": row["issue_number"],
                        "preview": row["message"][:120],
                        "source": "uncertain-acknowledged"
                        if row["id"] in was_uncertain
                        else "agent-checkpoint",
                    },
                )
        return sorted(set(tokens))

    async def held_receipt(
        self,
        session: str,
        message: str,
        sender: str,
        source_key: str | None,
        repo: str,
        issue: int | None,
        kind: str,
    ) -> dict | None:
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    "SELECT id,operation_id,status FROM message_queue WHERE session_name=:session "
                    "AND status IN ('pending','in_progress','checkpoint','uncertain') AND "
                    "((:kind='issue' AND delivery_kind='issue' "
                    "AND repo=:repo AND issue_number=:issue) "
                    "OR (:kind!='issue' AND dedup_key=:dedup)) LIMIT 1"
                ),
                {
                    "session": session,
                    "kind": kind,
                    "repo": repo,
                    "issue": issue,
                    "dedup": dedup_key_for(message, sender, source_key),
                },
            )
            row = result.mappings().first()
            return dict(row) if row else None

    async def retire_pending_briefs(self, session: str) -> int:
        """Expire the startup briefs still waiting for ``session``.

        Called before a launch, when no session of the agent exists: a brief
        queued for an earlier launch never reached it, and a new session must
        not receive it (#290). A leased row is retired too — the session it
        was leased for is gone, and a recovered lease would otherwise deliver
        it to the new one. Returns how many were retired.
        """
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    """UPDATE message_queue SET status = 'expired', delivered_at = :now
                       WHERE session_name = :session AND source = :brief
                         AND status IN ('pending', 'in_progress') RETURNING *"""
                ),
                {"now": now_iso(), "session": session, "brief": BRIEF_SOURCE},
            )
            rows = [dict(row) for row in result.mappings()]
            for row in rows:
                await self._ensure_operation_id(conn, row)
        for row in rows:
            await self._record_lifecycle(row, "retired_superseded_brief")
        return len(rows)

    async def has_brief_ahead(self, session: str, queue_id: int | None = None) -> bool:
        """Whether the session's startup brief, other than row ``queue_id``, is still to go."""
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    "SELECT 1 FROM message_queue WHERE session_name=:session AND source=:brief "
                    "AND status IN ('pending', 'in_progress') AND id != :id "
                    # A brief is never held behind another (a restart may queue a second).
                    "AND NOT EXISTS "
                    "(SELECT 1 FROM message_queue WHERE id = :id AND source = :brief) LIMIT 1"
                ),
                {
                    "session": session,
                    "brief": BRIEF_SOURCE,
                    "id": -1 if queue_id is None else queue_id,
                },
            )
            return result.first() is not None

    async def has_uncertain(self, session: str) -> bool:
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    "SELECT 1 FROM message_queue WHERE session_name=:session "
                    "AND status='uncertain' LIMIT 1"
                ),
                {"session": session},
            )
            return result.first() is not None
