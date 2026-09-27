"""Signed senders — the enrollment record, its owner-approved transitions,
seen nonces and a metadata-only audit (docs/owner-confirmed-messages.md).

The enrollment of a name (its key and epoch) changes only through
``apply_transition`` (the owner's Telegram approval) or ``rotate`` (a request
signed by the current key); there is no settings path to it.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

from agent_backbone.services.database._repo import Repo
from agent_backbone.services.database._time import now_iso

NONCE_RETENTION_SECONDS = 3600


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
        """Store a pending transition; a newer one supersedes the name's older one."""
        at = now_iso()
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "UPDATE signing_transitions SET status = 'superseded', resolved_at = :at"
                    " WHERE sender_key = :k AND status = 'pending'"
                ),
                {"k": sender_key, "at": at},
            )
            row = (
                await conn.execute(
                    text(
                        """INSERT INTO signing_transitions
                           (request_id, sender, sender_key, action, audience, expected_epoch,
                            new_public_key, new_fingerprint, digest, created_at, expires_at,
                            status)
                           VALUES (:request_id, :sender, :sender_key, :action, :audience,
                                   :expected_epoch, :new_public_key, :new_fingerprint, :digest,
                                   :at, :expires_at, 'pending')
                           RETURNING *"""
                    ),
                    {
                        "request_id": request_id,
                        "sender": sender,
                        "sender_key": sender_key,
                        "action": action,
                        "audience": audience,
                        "expected_epoch": expected_epoch,
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
        Applying a replace or clear retires the old key; a clear also releases
        the name."""
        at = now_iso()
        async with self._tx() as conn:
            row = (
                await conn.execute(
                    text("SELECT * FROM signing_transitions WHERE digest = :d"), {"d": digest}
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
            current = (
                await conn.execute(
                    text("SELECT epoch FROM signing_enrollments WHERE sender_key = :k"),
                    {"k": transition["sender_key"]},
                )
            ).fetchone()
            if (current[0] if current else 0) != transition["expected_epoch"]:
                return "epoch_changed", transition
            claimed = await conn.execute(
                text(
                    "UPDATE signing_transitions SET status = 'applied', resolved_at = :at,"
                    " resolved_by = :by WHERE id = :id AND status = 'pending'"
                ),
                {"id": transition["id"], "at": at, "by": by},
            )
            if claimed.rowcount != 1:
                return "not_pending", transition
            key = transition["sender_key"]
            await conn.execute(
                text(
                    "UPDATE signing_keys SET retired_at = :at, retired_by = :why"
                    " WHERE sender_key = :k AND retired_at IS NULL"
                ),
                {"k": key, "at": at, "why": transition["action"]},
            )
            await conn.execute(
                text("DELETE FROM signing_enrollments WHERE sender_key = :k"), {"k": key}
            )
            if transition["action"] != "clear":
                epoch = transition["expected_epoch"] + 1
                values = {
                    "k": key,
                    "sender": transition["sender"],
                    "pub": transition["new_public_key"],
                    "fp": transition["new_fingerprint"],
                    "epoch": epoch,
                    "at": at,
                }
                await conn.execute(
                    text(
                        "INSERT INTO signing_enrollments"
                        " (sender_key, sender, public_key, fingerprint, epoch, enrolled_at)"
                        " VALUES (:k, :sender, :pub, :fp, :epoch, :at)"
                    ),
                    values,
                )
                await conn.execute(
                    text(
                        "INSERT INTO signing_keys"
                        " (sender_key, epoch, sender, public_key, fingerprint, created_at)"
                        " VALUES (:k, :epoch, :sender, :pub, :fp, :at)"
                    ),
                    values,
                )
            await self._audit(
                conn,
                kind="transition",
                outcome="applied",
                sender_key=key,
                sender=transition["sender"],
                detail=f"{transition['action']} by {by}",
            )
        return "applied", transition

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
