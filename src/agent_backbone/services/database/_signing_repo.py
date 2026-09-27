"""Signed senders — the enrollment record, its owner-approved transitions,
seen nonces and a metadata-only audit (docs/owner-confirmed-messages.md).

The enrollment of a name (its key and epoch) changes only through
``apply_transition`` (the owner's Telegram approval) or ``rotate`` (a request
signed by the current key); there is no settings path to it.
"""

from __future__ import annotations

import time
import uuid

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from agent_backbone.services.database._queue_repo import _INSERT_COLUMNS
from agent_backbone.services.database._repo import Repo
from agent_backbone.services.database._time import cutoff_iso, now_iso

NONCE_RETENTION_SECONDS = 3600
CLAIM_SECONDS = 60
"""A steer offer's claim older than this is no longer held by its request."""
RECEIPT_RETENTION_DAYS = 90
_LIVE = "('pending', 'in_progress', 'checkpoint', 'uncertain')"
_IMMUTABLE = (
    "sender_key",
    "recipient",
    "kind",
    "text",
    "text_sha256",
    "source",
    "confirmed_at",
    "key_epoch",
)
_SETTLE_STEER = """UPDATE signing_receipts SET outcome = (
        SELECT d.outcome FROM deliveries d WHERE d.operation_id = signing_receipts.operation_id
        ORDER BY d.id DESC LIMIT 1)
    WHERE kind = 'steer' AND delivered_at IS NULL AND outcome IS NULL {where} AND (
        SELECT d.outcome FROM deliveries d WHERE d.operation_id = signing_receipts.operation_id
        ORDER BY d.id DESC LIMIT 1) IN ('not_taken', 'cancelled')"""
_SETTLE_MESSAGE = """UPDATE signing_receipts SET outcome = 'expired'
    WHERE kind = 'message' AND status = 'admitted' AND delivered_at IS NULL
      AND outcome IS NULL {where} AND EXISTS (
        SELECT 1 FROM message_queue q WHERE q.operation_id = signing_receipts.operation_id
          AND q.status = 'expired')"""
_NOT_LIVE = f"""NOT EXISTS (SELECT 1 FROM message_queue q
    WHERE q.operation_id = signing_receipts.operation_id AND q.status IN {_LIVE})"""
"""A receipt whose queued work still waits keeps its revocation link."""
_SETTLE_CLAIM = """UPDATE signing_receipts SET offer_state = CASE WHEN (
        SELECT d.outcome FROM deliveries d WHERE d.operation_id = signing_receipts.operation_id
        ORDER BY d.id DESC LIMIT 1) IN ('offered', 'handed_off', 'not_taken')
      THEN 'offered' ELSE 'failed' END
    WHERE kind = 'steer' AND offer_state = 'claiming' AND offer_claimed_at < :claim_cutoff
      {where} AND EXISTS (
        SELECT 1 FROM deliveries d WHERE d.operation_id = signing_receipts.operation_id)"""
"""Only a claim nobody holds any more (older than a minute): an active one is
settled by the request that holds it, once its offer is published or not."""
_SYNC_DELIVERED = """UPDATE signing_receipts SET delivered_at = (
        SELECT MIN(CASE WHEN d.outcome = 'handed_off'
          THEN COALESCE(d.settled_at, d.created_at) ELSE d.created_at END) FROM deliveries d
        WHERE d.operation_id = signing_receipts.operation_id
          AND d.outcome IN ('delivered', 'handed_off'))
    WHERE delivered_at IS NULL {where} AND EXISTS (
        SELECT 1 FROM deliveries d WHERE d.operation_id = signing_receipts.operation_id
          AND d.outcome IN ('delivered', 'handed_off'))"""


async def _settle(conn, where: str = "", params: dict | None = None) -> None:
    """Copy onto receipts what their queue and delivery records say (delivered,
    expired, not taken), so it outlives those records' shorter retention. A
    revoked receipt still records a handoff that happened (a steer offered
    before the reset can be taken): revocation and delivery are separate facts."""
    params = {**(params or {}), "claim_cutoff": int(time.time()) - CLAIM_SECONDS}
    for sql in (_SYNC_DELIVERED, _SETTLE_STEER, _SETTLE_MESSAGE, _SETTLE_CLAIM):
        await conn.execute(text(sql.format(where=where)), params)


async def _revoke_epoch(conn, sender_key: str, epoch: int, at: str) -> None:
    """A reset: the old epoch's confirmations that weren't delivered lose their
    authority, and their queued deliveries (leased ones too) are expired with
    a completion time, so ordinary retention removes their bodies."""
    params = {"k": sender_key, "epoch": epoch, "at": at}
    scope = "AND sender_key = :k AND key_epoch = :epoch"
    await _settle(conn, scope, params)
    await conn.execute(
        text(
            "UPDATE signing_receipts SET status = 'revoked', revoked_at = :at"
            f" WHERE status = 'admitted' AND delivered_at IS NULL {scope}"
        ),
        params,
    )
    # A paste that began without a recorded outcome is uncertain too.
    await conn.execute(
        text(
            "UPDATE message_queue SET status = 'uncertain', leased_at = NULL"
            " WHERE status IN ('pending', 'in_progress') AND operation_id IN (SELECT"
            " operation_id FROM signing_receipts WHERE status = 'revoked'"
            f" AND attempted_at IS NOT NULL {scope})"
        ),
        params,
    )
    # An uncertain row keeps its status: the paste may still sit in the input,
    # and that hold is what stops the next paste. What the inbox shows for it
    # no longer carries the marker, but says the confirmation was revoked.
    await conn.execute(
        text(
            "UPDATE message_queue SET message = REPLACE(message,"
            " ' owner-confirmed:' || (SELECT r.confirmation_id FROM signing_receipts r"
            " WHERE r.operation_id = message_queue.operation_id) || ']',"
            " '] (owner confirmation revoked: the sender''s key was reset)')"
            " WHERE status = 'uncertain' AND operation_id IN (SELECT operation_id"
            f" FROM signing_receipts WHERE status = 'revoked' {scope})"
        ),
        params,
    )
    await conn.execute(
        text(
            "UPDATE message_queue SET status = 'expired', delivered_at = :at"
            " WHERE status IN ('pending', 'in_progress', 'checkpoint')"
            " AND operation_id IN (SELECT operation_id"
            f" FROM signing_receipts WHERE status = 'revoked' {scope})"
        ),
        params,
    )


class _EpochChanged(Exception):
    """The enrollment moved under a transition; its transaction rolls back."""


def _one(row) -> dict | None:
    return dict(row._mapping) if row is not None else None


class SigningRepo(Repo):
    async def audience(self) -> str:
        """This install's audience id, created on first use and never changed."""
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "INSERT INTO signing_install (id, audience, created_at) "
                    "VALUES (1, :audience, :at) ON CONFLICT(id) DO NOTHING"
                ),
                {"audience": str(uuid.uuid4()), "at": now_iso()},
            )
            row = (await conn.execute(text("SELECT audience FROM signing_install"))).fetchone()
        return row[0]

    async def enrollment(self, sender_key: str) -> dict | None:
        async with self._tx() as conn:
            row = (
                await conn.execute(
                    text("SELECT * FROM signing_enrollments WHERE sender_key = :k"),
                    {"k": sender_key},
                )
            ).fetchone()
        return _one(row)

    async def pending(self, sender_key: str, now: int) -> dict | None:
        """The name's pending, unexpired transition, if any."""
        async with self._tx() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT * FROM signing_transitions WHERE sender_key = :k"
                        " AND status = 'pending' AND expires_at > :now ORDER BY id DESC LIMIT 1"
                    ),
                    {"k": sender_key, "now": now},
                )
            ).fetchone()
        return _one(row)

    async def watched(self, now: int) -> dict[str, dict]:
        """Every name that is enrolled or has a pending transition:
        ``{sender_key: {"enrollment": row | None, "pending": row | None}}``."""
        async with self._tx() as conn:
            enrolled = (await conn.execute(text("SELECT * FROM signing_enrollments"))).fetchall()
            pending = (
                await conn.execute(
                    text(
                        "SELECT * FROM signing_transitions WHERE status = 'pending'"
                        " AND expires_at > :now ORDER BY id"
                    ),
                    {"now": now},
                )
            ).fetchall()
        out: dict[str, dict] = {}
        for row in enrolled:
            out[row.sender_key] = {"enrollment": dict(row._mapping), "pending": None}
        for row in pending:
            out.setdefault(row.sender_key, {"enrollment": None, "pending": None})
            out[row.sender_key]["pending"] = dict(row._mapping)
        return out

    async def start_transition(
        self,
        *,
        sender: str,
        sender_key: str,
        action: str,
        audience: str,
        expected_epoch: int,
        new_public_key: str | None,
        new_fingerprint: str,
        request_id: str,
        digest: str,
        expires_at: int,
    ) -> dict:
        """Store a pending transition; a newer one supersedes the name's older one.

        A set or replace takes an epoch above any the name ever had
        (``new_epoch``), so a key enrolled after a clear never reuses one. At
        most one transition per name is pending (a unique index); when two
        starts race, the loser supersedes the winner on its one retry. A
        reused request id still raises IntegrityError."""
        try:
            return await self._start_transition(
                sender,
                sender_key,
                action,
                audience,
                expected_epoch,
                new_public_key,
                new_fingerprint,
                request_id,
                digest,
                expires_at,
            )
        except IntegrityError:
            return await self._start_transition(
                sender,
                sender_key,
                action,
                audience,
                expected_epoch,
                new_public_key,
                new_fingerprint,
                request_id,
                digest,
                expires_at,
            )

    async def _start_transition(
        self,
        sender,
        sender_key,
        action,
        audience,
        expected_epoch,
        new_public_key,
        new_fingerprint,
        request_id,
        digest,
        expires_at,
    ) -> dict:
        at = now_iso()
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "UPDATE signing_transitions SET status = 'superseded', resolved_at = :at"
                    " WHERE sender_key = :k AND status = 'pending'"
                ),
                {"k": sender_key, "at": at},
            )
            highest = (
                await conn.execute(
                    text("SELECT MAX(epoch) FROM signing_keys WHERE sender_key = :k"),
                    {"k": sender_key},
                )
            ).scalar()
            new_epoch = None if action == "clear" else max(highest or 0, expected_epoch) + 1
            row = (
                await conn.execute(
                    text(
                        """INSERT INTO signing_transitions
                           (request_id, sender, sender_key, action, audience, expected_epoch,
                            new_epoch, new_public_key, new_fingerprint, digest, created_at,
                            expires_at, status)
                           VALUES (:request_id, :sender, :sender_key, :action, :audience,
                                   :expected_epoch, :new_epoch, :new_public_key,
                                   :new_fingerprint, :digest, :at, :expires_at, 'pending')
                           RETURNING *"""
                    ),
                    {
                        "request_id": request_id,
                        "sender": sender,
                        "sender_key": sender_key,
                        "action": action,
                        "audience": audience,
                        "expected_epoch": expected_epoch,
                        "new_epoch": new_epoch,
                        "new_public_key": new_public_key,
                        "new_fingerprint": new_fingerprint,
                        "digest": digest,
                        "at": at,
                        "expires_at": expires_at,
                    },
                )
            ).fetchone()
        return dict(row._mapping)

    async def apply_transition(self, digest: str, *, now: int, by: str) -> tuple[str, dict | None]:
        """Apply the pending transition with this digest, once.

        Returns ``(outcome, transition)``: ``applied``, or ``unknown``,
        ``not_pending``, ``expired`` or ``epoch_changed`` with nothing changed.
        Every change to the enrollment is conditional on the expected epoch,
        so a rotation that commits first makes this ``epoch_changed``. A
        replace or clear retires the old key; a clear also releases the name."""
        at = now_iso()
        try:
            async with self._tx() as conn:
                row = (
                    await conn.execute(
                        text("SELECT * FROM signing_transitions WHERE digest = :d"),
                        {"d": digest},
                    )
                ).fetchone()
                if row is None:
                    return "unknown", None
                transition = dict(row._mapping)
                if transition["status"] != "pending":
                    return "not_pending", transition
                if transition["expires_at"] <= now:
                    await conn.execute(
                        text(
                            "UPDATE signing_transitions SET status = 'expired', resolved_at = :at"
                            " WHERE id = :id AND status = 'pending'"
                        ),
                        {"id": transition["id"], "at": at},
                    )
                    return "expired", transition
                claimed = await conn.execute(
                    text(
                        "UPDATE signing_transitions SET status = 'applied', resolved_at = :at,"
                        " resolved_by = :by WHERE id = :id AND status = 'pending'"
                    ),
                    {"id": transition["id"], "at": at, "by": by},
                )
                if claimed.rowcount != 1:
                    return "not_pending", transition
                await self._change_enrollment(conn, transition, at)
                await self._audit(
                    conn,
                    kind="transition",
                    outcome="applied",
                    sender_key=transition["sender_key"],
                    sender=transition["sender"],
                    detail=f"{transition['action']} by {by}",
                )
        except _EpochChanged:
            return "epoch_changed", transition
        return "applied", transition

    @staticmethod
    async def _change_enrollment(conn, transition: dict, at: str) -> None:
        """The conditional write; raises _EpochChanged (rolling back) when the
        enrollment is no longer at the expected epoch."""
        key = transition["sender_key"]
        expected = transition["expected_epoch"]
        values = {
            "k": key,
            "expected": expected,
            "sender": transition["sender"],
            "pub": transition["new_public_key"],
            "fp": transition["new_fingerprint"],
            "epoch": transition["new_epoch"],
            "at": at,
        }
        if transition["action"] == "set":
            changed = await conn.execute(
                text(
                    "INSERT INTO signing_enrollments"
                    " (sender_key, sender, public_key, fingerprint, epoch, enrolled_at)"
                    " VALUES (:k, :sender, :pub, :fp, :epoch, :at)"
                    " ON CONFLICT(sender_key) DO NOTHING"
                ),
                values,
            )
        elif transition["action"] == "replace":
            changed = await conn.execute(
                text(
                    "UPDATE signing_enrollments SET sender = :sender, public_key = :pub,"
                    " fingerprint = :fp, epoch = :epoch, enrolled_at = :at"
                    " WHERE sender_key = :k AND epoch = :expected"
                ),
                values,
            )
        else:
            changed = await conn.execute(
                text("DELETE FROM signing_enrollments WHERE sender_key = :k AND epoch = :expected"),
                values,
            )
        if changed.rowcount != 1:
            raise _EpochChanged
        await conn.execute(
            text(
                "UPDATE signing_keys SET retired_at = :at, retired_by = :why"
                " WHERE sender_key = :k AND retired_at IS NULL"
            ),
            {"k": key, "at": at, "why": transition["action"]},
        )
        if transition["action"] != "clear":
            await conn.execute(
                text(
                    "INSERT INTO signing_keys"
                    " (sender_key, epoch, sender, public_key, fingerprint, created_at)"
                    " VALUES (:k, :epoch, :sender, :pub, :fp, :at)"
                ),
                values,
            )
        if transition["action"] in ("replace", "clear") and expected:
            await _revoke_epoch(conn, key, expected, at)

    async def prune(self, days: int) -> int:
        """Drop refusal and observation rows older than ``days``; enrollment
        history (transitions, rotations, owner) stays."""
        cutoff = cutoff_iso(days=days)
        async with self._tx() as conn:
            gone = await conn.execute(
                text(
                    "DELETE FROM signing_audit WHERE kind IN ('refusal', 'observation')"
                    " AND at < :cutoff"
                ),
                {"cutoff": cutoff},
            )
        return gone.rowcount

    async def rotate(
        self, *, sender_key: str, old_epoch: int, new_public_key: str, new_fingerprint: str
    ) -> dict | None:
        """Replace the key signed for by the current one. None when the epoch moved."""
        at = now_iso()
        async with self._tx() as conn:
            moved = await conn.execute(
                text(
                    "UPDATE signing_enrollments SET public_key = :pub, fingerprint = :fp,"
                    " epoch = epoch + 1, enrolled_at = :at WHERE sender_key = :k AND epoch = :old"
                ),
                {
                    "k": sender_key,
                    "old": old_epoch,
                    "pub": new_public_key,
                    "fp": new_fingerprint,
                    "at": at,
                },
            )
            if moved.rowcount != 1:
                return None
            row = (
                await conn.execute(
                    text("SELECT * FROM signing_enrollments WHERE sender_key = :k"),
                    {"k": sender_key},
                )
            ).fetchone()
            await conn.execute(
                text(
                    "UPDATE signing_keys SET retired_at = :at, retired_by = 'rotate'"
                    " WHERE sender_key = :k AND retired_at IS NULL"
                ),
                {"k": sender_key, "at": at},
            )
            await conn.execute(
                text(
                    "INSERT INTO signing_keys"
                    " (sender_key, epoch, sender, public_key, fingerprint, created_at)"
                    " VALUES (:k, :epoch, :sender, :pub, :fp, :at)"
                ),
                {
                    "k": sender_key,
                    "epoch": row.epoch,
                    "sender": row.sender,
                    "pub": new_public_key,
                    "fp": new_fingerprint,
                    "at": at,
                },
            )
            await self._audit(
                conn,
                kind="rotation",
                outcome="applied",
                sender_key=sender_key,
                sender=row.sender,
                detail=f"epoch {old_epoch} -> {row.epoch}",
            )
        return dict(row._mapping)

    async def use_nonce(
        self, *, sender_key: str, epoch: int, nonce: str, request_hash: str, now: int
    ) -> tuple[str, str | None]:
        """Record a nonce. Returns ``("new", None)``, ``("replay", response)``
        for an identical retry, or ``("reused", None)`` for a different request."""
        async with self._tx() as conn:
            await conn.execute(
                text("DELETE FROM signing_nonces WHERE seen_at < :cutoff"),
                {"cutoff": now - NONCE_RETENTION_SECONDS},
            )
            inserted = await conn.execute(
                text(
                    "INSERT INTO signing_nonces (sender_key, epoch, nonce, request_hash, seen_at)"
                    " VALUES (:k, :epoch, :nonce, :hash, :now)"
                    " ON CONFLICT(sender_key, epoch, nonce) DO NOTHING"
                ),
                {"k": sender_key, "epoch": epoch, "nonce": nonce, "hash": request_hash, "now": now},
            )
            if inserted.rowcount == 1:
                return "new", None
            row = (
                await conn.execute(
                    text(
                        "SELECT request_hash, response FROM signing_nonces"
                        " WHERE sender_key = :k AND epoch = :epoch AND nonce = :nonce"
                    ),
                    {"k": sender_key, "epoch": epoch, "nonce": nonce},
                )
            ).fetchone()
        if row is not None and row.request_hash == request_hash:
            return "replay", row.response
        return "reused", None

    async def audit(
        self,
        *,
        kind: str,
        outcome: str,
        sender_key: str = "",
        sender: str = "",
        method: str = "",
        path: str = "",
        target: str = "",
        detail: str = "",
        operation_id: str | None = None,
    ) -> int:
        async with self._tx() as conn:
            return await self._audit(
                conn,
                kind=kind,
                outcome=outcome,
                sender_key=sender_key,
                sender=sender,
                method=method,
                path=path,
                target=target,
                detail=detail,
                operation_id=operation_id,
            )

    @staticmethod
    async def _audit(conn, **row) -> int:
        row.setdefault("sender_key", "")
        row.setdefault("sender", "")
        row.setdefault("method", "")
        row.setdefault("path", "")
        row.setdefault("target", "")
        row.setdefault("detail", "")
        row.setdefault("operation_id", None)
        result = await conn.execute(
            text(
                """INSERT INTO signing_audit
                   (at, kind, sender_key, sender, method, path, target, outcome, detail,
                    operation_id)
                   VALUES (:at, :kind, :sender_key, :sender, :method, :path, :target, :outcome,
                           :detail, :operation_id)
                   RETURNING seq"""
            ),
            {"at": now_iso(), **row},
        )
        return result.scalar_one()

    async def observations(self, sender_key: str, after: int, limit: int) -> list[dict]:
        async with self._tx() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT seq, at, method, path, outcome FROM signing_audit"
                        " WHERE sender_key = :k AND kind = 'observation' AND seq > :after"
                        " ORDER BY seq LIMIT :limit"
                    ),
                    {"k": sender_key, "after": after, "limit": limit},
                )
            ).fetchall()
        return [dict(r._mapping) for r in rows]

    async def observation_counts(self, sender_key: str, since: str) -> dict[str, int]:
        """How requests made as the name fared since ``since`` (ISO time)."""
        async with self._tx() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT outcome, COUNT(*) FROM signing_audit WHERE sender_key = :k"
                        " AND kind = 'observation' AND at >= :since GROUP BY outcome"
                    ),
                    {"k": sender_key, "since": since},
                )
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    async def admit(
        self,
        *,
        nonce: str,
        request_hash: str,
        now: int,
        receipt: dict,
        queue: dict | None,
        fresh: bool = True,
    ) -> tuple[str, dict | None]:
        """Commit a confirmation in one transaction: the nonce, the receipt and,
        for a message, its queued delivery.

        Returns ``("admitted", receipt)``; ``("replay", receipt)`` for an
        identical retry with the same nonce; ``("recovered", receipt)`` for the
        same confirmation, unchanged, under a fresh nonce; ``("nonce_reused",
        None)``; ``("conflict", None)`` when the confirmation id is taken by
        a different confirmation; ``("epoch_changed", None)`` when the key was
        reset since the request was checked; or ``("expired", None)`` for a new
        confirmation that isn't ``fresh`` (an identical one is still
        recovered). Nothing is written unless admitted or recovered."""
        key, epoch = receipt["sender_key"], receipt["key_epoch"]
        async with self._tx() as conn:
            await conn.execute(  # also takes SQLite's write lock for what follows
                text("DELETE FROM signing_nonces WHERE seen_at < :cutoff"),
                {"cutoff": now - NONCE_RETENTION_SECONDS},
            )
            lock = " FOR UPDATE" if conn.dialect.name == "postgresql" else ""
            current = (
                await conn.execute(
                    text(f"SELECT epoch FROM signing_enrollments WHERE sender_key = :k{lock}"),
                    {"k": key},
                )
            ).scalar()
            if current != epoch:  # reset after the request was checked
                return "epoch_changed", None
            seen = (
                await conn.execute(
                    text(
                        "SELECT request_hash FROM signing_nonces"
                        " WHERE sender_key = :k AND epoch = :epoch AND nonce = :nonce"
                    ),
                    {"k": key, "epoch": epoch, "nonce": nonce},
                )
            ).scalar()
            existing = (
                await conn.execute(
                    text("SELECT * FROM signing_receipts WHERE confirmation_id = :c"),
                    {"c": receipt["confirmation_id"]},
                )
            ).fetchone()
            existing = dict(existing._mapping) if existing is not None else None
            same = existing is not None and all(existing[f] == receipt[f] for f in _IMMUTABLE)
            if seen is not None:
                if seen == request_hash and same:
                    return "replay", existing
                return "nonce_reused", None
            if existing is not None and not same:
                return "conflict", None
            if existing is None and not fresh:
                return "expired", None
            await conn.execute(
                text(
                    "INSERT INTO signing_nonces (sender_key, epoch, nonce, request_hash, seen_at)"
                    " VALUES (:k, :epoch, :nonce, :hash, :now)"
                ),
                {"k": key, "epoch": epoch, "nonce": nonce, "hash": request_hash, "now": now},
            )
            if existing is not None:
                return "recovered", existing
            row = (
                await conn.execute(
                    text(
                        """INSERT INTO signing_receipts
                           (confirmation_id, sender, sender_key, recipient, delivered_to, kind,
                            text, text_sha256, source, confirmed_at, key_epoch, operation_id,
                            status, created_at)
                           VALUES (:confirmation_id, :sender, :sender_key, :recipient,
                                   :delivered_to, :kind, :text, :text_sha256, :source,
                                   :confirmed_at, :key_epoch, :operation_id, 'admitted', :at)
                           RETURNING *"""
                    ),
                    {**receipt, "at": now_iso()},
                )
            ).fetchone()
            if queue is not None:
                await conn.execute(
                    text(f"INSERT INTO message_queue {_INSERT_COLUMNS}"),
                    {
                        "operation_id": receipt["operation_id"],
                        "repo": "",
                        "issue_number": None,
                        "delivery_kind": "direct_message",
                        "enqueued_at": now_iso(),
                        "initial_status": "pending",
                        "dedup_key": f"src:confirmation:{receipt['confirmation_id']}",
                        **queue,
                    },
                )
        return "admitted", dict(row._mapping)

    async def is_confirmation(self, operation_id: str | None) -> bool:
        """Whether an operation delivers an owner confirmation."""
        if not operation_id:
            return False
        async with self._tx() as conn:
            row = (
                await conn.execute(
                    text("SELECT 1 FROM signing_receipts WHERE operation_id = :op"),
                    {"op": operation_id},
                )
            ).fetchone()
        return row is not None

    async def was_delivered(self, operation_id: str | None) -> bool:
        """Whether a successful delivery of this confirmation was recorded, in
        the delivery records or on the receipt, which outlives them."""
        if not operation_id:
            return False
        async with self._tx() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM deliveries WHERE operation_id = :op"
                        " AND outcome IN ('delivered', 'handed_off')"
                        " UNION ALL SELECT 1 FROM signing_receipts WHERE operation_id = :op"
                        " AND delivered_at IS NOT NULL"
                    ),
                    {"op": operation_id},
                )
            ).fetchone()
        return row is not None

    async def attempt(self, operation_id: str, *, begun: bool) -> None:
        """Record that a message's paste begins (``begun``), or that it
        definitely didn't reach the terminal."""
        async with self._tx() as conn:
            await conn.execute(
                text("UPDATE signing_receipts SET attempted_at = :at WHERE operation_id = :op"),
                {"at": now_iso() if begun else None, "op": operation_id},
            )

    async def was_attempted(self, operation_id: str | None) -> bool:
        """Whether a paste of this confirmation began without a recorded outcome."""
        if not operation_id:
            return False
        async with self._tx() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM signing_receipts WHERE operation_id = :op"
                        " AND attempted_at IS NOT NULL"
                    ),
                    {"op": operation_id},
                )
            ).fetchone()
        return row is not None

    async def is_revoked(self, operation_id: str) -> bool:
        async with self._tx() as conn:
            status = (
                await conn.execute(
                    text("SELECT status FROM signing_receipts WHERE operation_id = :op"),
                    {"op": operation_id},
                )
            ).scalar()
        return status == "revoked"

    async def claim_offer(self, confirmation_id: str, now: int) -> str | None:
        """Claim the one offer of a confirmed steer: a token only its holder
        can publish, mark offered or discard with. None when it was offered
        already, or another request is offering it now. A claim older than a
        minute whose offer was recorded is settled from that, not offered again."""
        async with self._tx() as conn:
            await _settle(conn, "AND confirmation_id = :c", {"c": confirmation_id})
            row = (
                await conn.execute(
                    text(
                        "SELECT operation_id, offer_state, offer_claimed_at, delivered_at, outcome"
                        " FROM signing_receipts WHERE confirmation_id = :c AND status = 'admitted'"
                    ),
                    {"c": confirmation_id},
                )
            ).fetchone()
            if row is None or row.offer_state in ("offered", "failed"):
                return None
            if row.delivered_at is not None or row.outcome is not None:
                # Its attempt is on record already: settle the claim from it.
                reached = row.delivered_at is not None or row.outcome == "not_taken"
                await conn.execute(
                    text(
                        "UPDATE signing_receipts SET offer_state = :state"
                        " WHERE confirmation_id = :c"
                    ),
                    {"c": confirmation_id, "state": "offered" if reached else "failed"},
                )
                return None
            if row.offer_state == "claiming":
                if row.offer_claimed_at > now - CLAIM_SECONDS:
                    return None
                recorded = (
                    await conn.execute(
                        text(
                            "SELECT outcome FROM deliveries WHERE operation_id = :op"
                            " ORDER BY id DESC LIMIT 1"
                        ),
                        {"op": row.operation_id},
                    )
                ).scalar()
                if recorded is not None:  # the stale claim got as far as an attempt
                    reached = recorded in ("offered", "handed_off", "not_taken")
                    await conn.execute(
                        text(
                            "UPDATE signing_receipts SET offer_state = :state"
                            " WHERE confirmation_id = :c"
                        ),
                        {"c": confirmation_id, "state": "offered" if reached else "failed"},
                    )
                    return None
            token = uuid.uuid4().hex
            claimed = await conn.execute(
                text(
                    "UPDATE signing_receipts SET offer_state = 'claiming',"
                    " offer_claimed_at = :now, offer_token = :token WHERE confirmation_id = :c"
                    " AND (offer_state IS NULL OR offer_claimed_at = :was)"
                ),
                {"c": confirmation_id, "now": now, "was": row.offer_claimed_at, "token": token},
            )
        return token if claimed.rowcount == 1 else None

    async def holds_claim(self, confirmation_id: str, token: str) -> bool:
        """Whether ``token`` still holds the offer's claim (none replaced it)."""
        async with self._tx() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM signing_receipts WHERE confirmation_id = :c"
                        " AND offer_state = 'claiming' AND offer_token = :t"
                    ),
                    {"c": confirmation_id, "t": token},
                )
            ).fetchone()
        return row is not None

    async def mark_offered(self, confirmation_id: str, token: str) -> None:
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "UPDATE signing_receipts SET offer_state = 'offered'"
                    " WHERE confirmation_id = :c AND offer_token = :t"
                ),
                {"c": confirmation_id, "t": token},
            )

    async def discard(self, confirmation_id: str, token: str) -> None:
        """Forget a confirmation that was never offered (a refused steer), for
        the request holding its claim."""
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "DELETE FROM signing_receipts WHERE confirmation_id = :c"
                    " AND delivered_at IS NULL AND offer_token = :t"
                    " AND (offer_state IS NULL OR offer_state IN ('claiming', 'failed'))"
                ),
                {"c": confirmation_id, "t": token},
            )

    async def receipt(self, confirmation_id: str) -> dict | None:
        async with self._tx() as conn:
            await _settle(conn, "AND confirmation_id = :c", {"c": confirmation_id})
            row = (
                await conn.execute(
                    text("SELECT * FROM signing_receipts WHERE confirmation_id = :c"),
                    {"c": confirmation_id},
                )
            ).fetchone()
        return dict(row._mapping) if row is not None else None

    async def receipts(self, sender_key: str, after: int, limit: int) -> dict:
        """A sender's receipts after ``after``, and what retention removed."""
        async with self._tx() as conn:
            await _settle(conn, "AND sender_key = :k", {"k": sender_key})
            rows = (
                await conn.execute(
                    text(
                        "SELECT * FROM signing_receipts WHERE sender_key = :k AND seq > :after"
                        " ORDER BY seq LIMIT :limit"
                    ),
                    {"k": sender_key, "after": after, "limit": limit},
                )
            ).fetchall()
            oldest = (
                await conn.execute(
                    text("SELECT MIN(seq) FROM signing_receipts WHERE sender_key = :k"),
                    {"k": sender_key},
                )
            ).scalar()
            pruned = (
                await conn.execute(
                    text(
                        "SELECT pruned_through FROM signing_receipt_watermarks"
                        " WHERE sender_key = :k"
                    ),
                    {"k": sender_key},
                )
            ).scalar()
        return {
            "rows": [dict(r._mapping) for r in rows],
            "oldest_seq": oldest,
            "pruned_through": pruned or 0,
        }

    async def prune_receipts(self) -> int:
        """Drop receipts older than the retention period, remembering per sender
        the highest seq removed, so a reader can tell a gap from nothing new.
        Delivery times are settled first, from the deliveries still kept."""
        cutoff = cutoff_iso(days=RECEIPT_RETENTION_DAYS)
        async with self._tx() as conn:
            await _settle(conn)
            await conn.execute(
                text(
                    f"""INSERT INTO signing_receipt_watermarks (sender_key, pruned_through)
                       SELECT sender_key, MAX(seq) FROM signing_receipts
                       WHERE created_at < :cutoff AND {_NOT_LIVE} GROUP BY sender_key
                       ON CONFLICT(sender_key) DO UPDATE SET pruned_through = CASE
                         WHEN excluded.pruned_through > signing_receipt_watermarks.pruned_through
                         THEN excluded.pruned_through
                         ELSE signing_receipt_watermarks.pruned_through END"""
                ),
                {"cutoff": cutoff},
            )
            gone = await conn.execute(
                text(f"DELETE FROM signing_receipts WHERE created_at < :cutoff AND {_NOT_LIVE}"),
                {"cutoff": cutoff},
            )
        return gone.rowcount

    async def owner(self) -> dict:
        """``{telegram_user_id, pending_user_id}``; both None when never set."""
        async with self._tx() as conn:
            row = (
                await conn.execute(
                    text("SELECT telegram_user_id, pending_user_id FROM signing_owner")
                )
            ).fetchone()
        if row is None:
            return {"telegram_user_id": None, "pending_user_id": None}
        return {"telegram_user_id": row[0], "pending_user_id": row[1]}

    async def set_owner(self, user_id: int) -> str:
        """Set the owner's Telegram id when none is set (``set``); otherwise
        record it as a change the current owner must approve (``pending``)."""
        at = now_iso()
        async with self._tx() as conn:
            inserted = await conn.execute(
                text(
                    "INSERT INTO signing_owner (id, telegram_user_id, set_at)"
                    " VALUES (1, :uid, :at) ON CONFLICT(id) DO NOTHING"
                ),
                {"uid": user_id, "at": at},
            )
            if inserted.rowcount == 1:
                await self._audit(conn, kind="owner", outcome="set", detail=str(user_id))
                return "set"
            await conn.execute(
                text(
                    "UPDATE signing_owner SET pending_user_id = :uid, pending_at = :at WHERE id = 1"
                ),
                {"uid": user_id, "at": at},
            )
            await self._audit(conn, kind="owner", outcome="change_requested", detail=str(user_id))
        return "pending"

    async def approve_owner_change(self, *, by_user_id: int, new_user_id: int) -> bool:
        """The current owner approves the pending change to ``new_user_id``."""
        at = now_iso()
        async with self._tx() as conn:
            changed = await conn.execute(
                text(
                    "UPDATE signing_owner SET telegram_user_id = pending_user_id, set_at = :at,"
                    " pending_user_id = NULL, pending_at = NULL WHERE id = 1"
                    " AND telegram_user_id = :by AND pending_user_id = :new"
                ),
                {"by": by_user_id, "new": new_user_id, "at": at},
            )
            if changed.rowcount != 1:
                return False
            await self._audit(
                conn, kind="owner", outcome="changed", detail=f"{by_user_id} -> {new_user_id}"
            )
        return True
