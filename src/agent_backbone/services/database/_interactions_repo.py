"""Read-only interaction projection with authenticated, scoped snapshot cursors."""

from __future__ import annotations

import base64
import hmac
import json
import secrets

from sqlalchemy import and_, case, exists, func, or_, select, text, update

from agent_backbone.models import RETIREMENT_REASONS
from agent_backbone.services.database import _interaction_observations as observations
from agent_backbone.services.database._interaction_capture import capture
from agent_backbone.services.database._repo import Repo
from agent_backbone.services.database._time import cutoff_iso, now_iso
from agent_backbone.services.database.models import (
    AgentORM,
    ConversationEntryORM,
    InteractionCaptureCheckpointORM,
    InteractionCoverageORM,
    InteractionEventORM,
    InteractionORM,
    RegisteredStateObservationORM,
)

_A = AgentORM.__table__
_C = ConversationEntryORM.__table__
_I = InteractionORM.__table__
_E = InteractionEventORM.__table__
_COVER = InteractionCoverageORM.__table__
_DELIVERED = {"delivered", "acknowledged", "handed_off"}
_TERMINAL = (
    _DELIVERED
    | RETIREMENT_REASONS
    | {"cancelled", "not_taken", "expired", "registration_retired", "failed"}
)
_SCOPE = ["backbone_messages", "steers", "artifact_conversations", "owner_questions"]


class InteractionRepo(Repo):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._key = secrets.token_bytes(32)

    async def capture(self, **kwargs) -> str:
        async with self._tx() as conn:
            return await capture(conn, **kwargs)

    def _encode(self, value: dict) -> str:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return (
            base64.urlsafe_b64encode(raw).decode()
            + "."
            + hmac.digest(self._key, raw, "sha256").hex()
        )

    def _decode(self, value: str) -> dict:
        try:
            encoded, signature = value.rsplit(".", 1)
            raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
            if not hmac.compare_digest(hmac.digest(self._key, raw, "sha256").hex(), signature):
                raise ValueError
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (ValueError, TypeError) as exc:
            raise ValueError("Invalid or expired interaction cursor; refresh the view") from exc

    async def _snapshot(self, conn, since, until, token=None):
        if token:
            snapshot = self._decode(token)
            if snapshot.get("window") != [since, until] or snapshot.get("kind") != "snapshot":
                raise ValueError("Snapshot belongs to a different time window")
        else:
            if conn.dialect.name == "postgresql":
                # Drain earlier sequence allocations before freezing the watermark.
                await conn.execute(
                    text(
                        "LOCK TABLE interactions, interaction_events, "
                        "registered_state_observations, "
                        "interaction_capture_checkpoints IN SHARE MODE"
                    )
                )
            snapshot = {
                "kind": "snapshot",
                "window": [since, until],
                "high": await conn.scalar(select(func.max(_E.c.id))) or 0,
                "state_high": await conn.scalar(select(func.max(RegisteredStateObservationORM.id)))
                or 0,
                "checkpoint_high": await conn.scalar(
                    select(func.max(InteractionCaptureCheckpointORM.id))
                )
                or 0,
                "at": now_iso(),
            }
            token = self._encode(snapshot)
        return snapshot, token

    async def _coverage(self, conn, since, until, snapshot, *, truncated=False):
        row = (await conn.execute(select(_COVER))).mappings().first()
        start = row["capture_started_at"] if row else None
        retained = row["retained_from"] if row else None
        reasons = []
        if not start:
            reasons.append("no_captured_history")
        elif since < start:
            reasons.append("window_precedes_capture")
        if retained and since < retained:
            reasons.append("body_retention_partial")
        if truncated:
            reasons.append("projection_limit")
        (
            intervals,
            complete,
            interval_limit,
            interval_reasons,
        ) = await observations.message_intervals(conn, since, until, snapshot)
        reasons.extend(interval_reasons)
        # An unattributed sender might be this registration. Do not certify a
        # per-agent quiet span over its recorded lifecycle by guessing a name.
        projected = self._projection(snapshot["high"], bodies=False, until=until).subquery()
        ambiguous = (
            (
                await conn.execute(
                    select(
                        projected.c.sender,
                        projected.c.recipient,
                        projected.c.accepted_at,
                        projected.c.ended_at,
                    )
                    .where(
                        projected.c.accepted_at < until,
                        or_(projected.c.last_event_at >= since, projected.c.ended_at.is_(None)),
                    )
                    .limit(10001)
                )
            )
            .mappings()
            .all()
        )
        if len(ambiguous) > 10000:
            intervals = []
            interval_limit = True
            reasons.append("attribution_projection_limit")
        else:
            gaps = []
            for item in ambiguous:
                actors = [json.loads(item[column]) for column in ("sender", "recipient")]
                if any(a["kind"] == "unknown" or a["attribution"] == "unverified" for a in actors):
                    gap_start, gap_end = (
                        max(since, item["accepted_at"]),
                        min(until, item["ended_at"] or until),
                    )
                    if gap_start < gap_end:
                        gaps.append((gap_start, gap_end))
            if gaps:
                reasons.append("unverified_actor_attribution")
                intervals = observations.exclude_intervals(intervals, gaps)
        complete = (
            not interval_limit
            and len(intervals) == 1
            and intervals[0]["since"] == since
            and intervals[0]["until"] == until
        )
        if not complete:
            reasons.append("legacy_history_not_reconstructed")
        return {
            "since": since,
            "until": until,
            "capture_started_at": start,
            "retained_from": start,
            "body_retained_from": retained,
            "as_of": snapshot["at"],
            "completeness": "complete_within_declared_scope"
            if complete and not truncated
            else "partial"
            if start
            else "unavailable",
            "message_intervals": intervals,
            "quiet_scope": "capture_enabled_backbone_paths",
            "reasons": reasons,
            "declared_scope": _SCOPE,
            "truncated": truncated or interval_limit,
            "state_history": "observed_partial",
        }

    def _projection(self, high, *, bodies=True, until=None):
        in_window = _E.c.time < until if until else _E.c.id <= high
        aggregate = (
            select(
                _E.c.interaction_id,
                func.max(_E.c.time).filter(in_window).label("last_event_at"),
                func.max(_E.c.id).filter(_E.c.outcome.is_not(None), in_window).label("state_id"),
                func.max(case((_E.c.outcome.in_(_DELIVERED), 1), else_=0)).label("delivered"),
            )
            .where(_E.c.id <= high)
            .group_by(_E.c.interaction_id)
            .subquery()
        )
        state = _E.alias("last_transport_event")
        return (
            select(
                *[c for c in _I.c if bodies or c.name not in {"body", "preview", "relations"}],
                aggregate.c.last_event_at,
                aggregate.c.delivered,
                case((state.c.outcome.in_(_TERMINAL), state.c.time), else_=None).label("ended_at"),
            )
            .join(aggregate, aggregate.c.interaction_id == _I.c.id)
            .outerjoin(state, state.c.id == aggregate.c.state_id)
        )

    @staticmethod
    def _agent_filter(agent_id, direction):
        if direction == "incoming":
            return _I.c.recipient_id == agent_id
        if direction == "outgoing":
            return _I.c.sender_id == agent_id
        return or_(_I.c.sender_id == agent_id, _I.c.recipient_id == agent_id)

    async def _actors(self, conn):
        return {
            r["report_identity"]: dict(r)
            for r in (await conn.execute(select(_A))).mappings()
            if r["report_identity"]
        }

    @staticmethod
    def _actor(value, actors):
        actor = json.loads(value) if isinstance(value, str) else dict(value)
        if actor["kind"] == "agent":
            row = actors.get(actor["id"])
            actor["name"] = row["name"] if row else actor["name_at_capture"]
            actor["retired"] = row is None
        return actor

    async def _events(self, conn, interaction_id, high):
        return [
            dict(r)
            for r in (
                await conn.execute(
                    select(_E)
                    .where(_E.c.interaction_id == interaction_id, _E.c.id <= high)
                    .order_by(_E.c.id)
                )
            ).mappings()
        ]

    @staticmethod
    def _status(events):
        events = [e for e in events if e["kind"] != "discussion"]
        success = [e for e in events if e["outcome"] in _DELIVERED]
        event = success[-1] if success else events[-1] if events else None
        value = event["outcome"] or event["kind"] if event else "unknown"
        if value == "accepted":
            value = "unknown"
        return {
            "value": value,
            "observed_at": event["time"] if event else None,
            "evidence": "recorded transport observation" if event else "not recorded",
            "delivered": bool(success),
        }

    async def _conversation(self, conn, row, actors):
        recipients = []
        for column in ("sender", "recipient"):
            actor = self._actor(row[column], actors)
            # An asserted name does not grant access to private message context.
            if (
                actor["kind"] == "agent"
                and actor["attribution"] != "unverified"
                and not actor["retired"]
            ) and all(a["id"] != actor["id"] for a in recipients):
                recipients.append(actor)
        count, last = (
            await conn.execute(
                select(func.count(), func.max(_C.c.created_at)).where(
                    _C.c.kind == "interaction",
                    _C.c.artifact_id == row["id"],
                    _C.c.entry_kind == "message",
                )
            )
        ).one()
        return {
            "supported": True,
            "kind": "interaction",
            "artifact_id": row["id"],
            "recipients": recipients,
            "reason": None if recipients else "no_current_verified_participant",
            "total": count,
            "last_entry_at": last,
        }

    def _event_view(self, e):
        return {
            "id": str(e["id"]),
            "kind": e["kind"],
            "time": e["time"],
            "time_basis": e["time_basis"],
            "outcome": e["outcome"],
            "attempt_id": f"delivery:{e['delivery_id']}" if e["delivery_id"] else None,
            "source": e["source"],
        }

    def _attempts(self, events):
        result = {}
        for e in events:
            if e["delivery_id"] is None:
                continue
            key = f"delivery:{e['delivery_id']}"
            if key not in result:
                result[key] = {
                    "id": key,
                    "operation_id": e["operation_id"],
                    "delivery_id": e["delivery_id"],
                    "queue_id": e["queue_id"],
                    "outcome": e["outcome"] or "unknown",
                    "source": e["source"],
                    "recorded_at": e["time"],
                    "settled_at": None,
                }
            else:
                result[key].update(
                    outcome=e["outcome"] or result[key]["outcome"], settled_at=e["time"]
                )
        return list(result.values())

    def _subpage(self, items, interaction_id, kind, high, cursor=None, limit=50):
        offset = 0
        if cursor:
            data = self._decode(cursor)
            if data.get("kind") != kind or data.get("interaction") != interaction_id:
                raise ValueError("Cursor belongs to another interaction or projection")
            offset = data["offset"]
            if data["high"] != high:
                raise ValueError("Snapshot mismatch")
        selected = items[offset : offset + limit]
        next_cursor = (
            self._encode(
                {
                    "kind": kind,
                    "interaction": interaction_id,
                    "high": high,
                    "offset": offset + limit,
                }
            )
            if len(items) > offset + limit
            else None
        )
        return {"items": selected, "next_cursor": next_cursor}

    async def _conversation_entry(self, conn, row):
        # Only capture-generated links can identify the source entry. Sender-
        # supplied links are references, not proof that this is that entry.
        for relation in json.loads(row["relations"]):
            if relation.get("provenance") != "conversation_entry":
                continue
            target = relation["target"]
            entry = (
                (
                    await conn.execute(
                        select(_C).where(
                            _C.c.id == target.get("entry_id"),
                            _C.c.kind == target.get("artifact_kind"),
                            _C.c.artifact_id == target["id"],
                        )
                    )
                )
                .mappings()
                .first()
            )
            if entry:
                return {
                    key: entry[key]
                    for key in (
                        "kind",
                        "artifact_id",
                        "entry_kind",
                        "created_at",
                        "author_type",
                        "author_id",
                        "author_name",
                        "recipient_id",
                        "recipient_name",
                        "in_reply_to",
                    )
                } | {
                    "entry_id": entry["id"],
                    "anchor": json.loads(entry["anchor"]) if entry["anchor"] else None,
                    "anchored_interaction_id": entry["artifact_id"]
                    if entry["kind"] == "interaction"
                    else None,
                }
        return None

    async def _record(self, conn, row, high, actors):
        events = await self._events(conn, row["id"], high)
        attempts = self._attempts(events)
        relations = [r | {"source_interaction_id": row["id"]} for r in json.loads(row["relations"])]
        # A reverse link is a projection of an explicitly recorded relation,
        # never an inference from content or timing. Its source remains explicit.
        referring = (
            await conn.execute(
                select(_I.c.id, _I.c.relations)
                .where(
                    _I.c.id != row["id"],
                    _I.c.relations.contains(row["id"]),
                    exists(select(_E.c.id).where(_E.c.interaction_id == _I.c.id, _E.c.id <= high)),
                )
                .limit(201)
            )
        ).all()
        for source in referring[:200]:
            for relation in json.loads(source.relations):
                if (
                    relation["target"]["kind"] in {"interaction", "owner_request"}
                    and relation["target"]["id"] == row["id"]
                ):
                    relations.append(relation | {"source_interaction_id": source.id})
        return {
            "id": row["id"],
            "operation_id": row["operation_id"],
            "accepted_at": row["accepted_at"],
            "last_event_at": events[-1]["time"] if events else row["accepted_at"],
            "kind": row["kind"],
            "sender": self._actor(row["sender"], actors),
            "recipient": self._actor(row["recipient"], actors),
            "original_target": row["original_target"],
            "preview": row["preview"],
            "body": {
                "level": "full" if row["body"] is not None else "excerpt",
                "text": row["body"] if row["body"] is not None else row["preview"],
                "reason": None if row["body"] is not None else "retention",
                "format": row["body_format"],
            },
            "status": self._status(events),
            "attempt_count": len(attempts),
            "attempts": self._subpage(attempts, row["id"], "attempts", high),
            "lifecycle": self._subpage(
                [self._event_view(e) for e in events], row["id"], "events", high
            ),
            "relations": relations,
            "relations_truncated": len(referring) > 200,
            "conversation": await self._conversation(conn, row, actors),
            "conversation_entry": await self._conversation_entry(conn, row),
        }

    async def query(
        self, *, agent_id, since, until, direction="both", limit=50, cursor=None, snapshot=None
    ):
        async with self._tx() as conn:
            if cursor:
                data = self._decode(cursor)
                if (
                    data.get("scope") != [agent_id, since, until, direction]
                    or data.get("kind") != "history"
                ):
                    raise ValueError("Cursor belongs to different filters")
                if snapshot and snapshot != data["snapshot"]:
                    raise ValueError("Snapshot mismatch")
                snapshot = data["snapshot"]
            snap, token = await self._snapshot(conn, since, until, snapshot)
            projected = self._projection(snap["high"], until=until).subquery()
            q = select(projected).where(
                projected.c.accepted_at < until,
                or_(projected.c.last_event_at >= since, projected.c.ended_at.is_(None)),
            )
            col = projected.c
            q = q.where(
                col.recipient_id == agent_id
                if direction == "incoming"
                else col.sender_id == agent_id
                if direction == "outgoing"
                else or_(col.sender_id == agent_id, col.recipient_id == agent_id)
            )
            if cursor:
                q = q.where(
                    or_(
                        col.accepted_at < data["at"],
                        and_(col.accepted_at == data["at"], col.id < data["id"]),
                    )
                )
            rows = [
                dict(r)
                for r in (
                    await conn.execute(
                        q.order_by(col.accepted_at.desc(), col.id.desc()).limit(limit + 1)
                    )
                ).mappings()
            ]
            more = len(rows) > limit
            rows = rows[:limit]
            actors = await self._actors(conn)
            items = []
            page_bytes = 0
            for row in rows:
                item = await self._record(conn, row, snap["high"], actors)
                size = len(json.dumps(item).encode())
                if items and page_bytes + size > 1_048_576:
                    more = True
                    break
                items.append(item)
                page_bytes += size
            rows = rows[: len(items)]
            return {
                "schema_version": 1,
                "items": items,
                "next_cursor": self._encode(
                    {
                        "kind": "history",
                        "scope": [agent_id, since, until, direction],
                        "snapshot": token,
                        "at": rows[-1]["accepted_at"],
                        "id": rows[-1]["id"],
                    }
                )
                if more
                else None,
                "snapshot": token,
                "coverage": await self._coverage(conn, since, until, snap),
            }

    async def detail(self, interaction_id):
        async with self._tx() as conn:
            row = (
                (await conn.execute(select(_I).where(_I.c.id == interaction_id))).mappings().first()
            )
            if row is None:
                return None
            high = await conn.scalar(select(func.max(_E.c.id))) or 0
            return await self._record(conn, row, high, await self._actors(conn))

    async def subpage(self, interaction_id, kind, cursor=None, limit=50):
        async with self._tx() as conn:
            row = (
                (await conn.execute(select(_I).where(_I.c.id == interaction_id))).mappings().first()
            )
            if row is None:
                return None
            if cursor:
                data = self._decode(cursor)
                # Another projection's cursor is refused before its fields are read.
                if (
                    data.get("kind") != kind
                    or data.get("interaction") != interaction_id
                    or not isinstance(data.get("high"), int)
                ):
                    raise ValueError("Cursor belongs to another interaction or projection")
                high = data["high"]
            else:
                high = await conn.scalar(select(func.max(_E.c.id))) or 0
            events = await self._events(conn, interaction_id, high)
            items = (
                self._attempts(events)
                if kind == "attempts"
                else [self._event_view(e) for e in events]
            )
            return self._subpage(items, interaction_id, kind, high, cursor, limit)

    async def summary(self, *, since, until, agent_id=None, snapshot=None):
        async with self._tx() as conn:
            snap, token = await self._snapshot(conn, since, until, snapshot)
            actors = await self._actors(conn)
            rows = [
                dict(r)
                for r in (
                    await conn.execute(
                        self._projection(snap["high"], bodies=False, until=until)
                        .where(_I.c.accepted_at < until)
                        .order_by(_I.c.accepted_at.desc(), _I.c.id.desc())
                        .limit(10001)
                    )
                ).mappings()
            ]
            truncated = len(rows) > 10000
            rows = rows[:10000]
            summaries = {}
            for identity, row in actors.items():
                if agent_id and agent_id != identity:
                    continue
                a = {
                    "kind": "agent",
                    "id": identity,
                    "name": row["name"],
                    "name_at_capture": row["name"],
                    "attribution": "system",
                    "retired": False,
                }
                summaries[identity] = {
                    "agent": a,
                    "runtime": row["runtime"],
                    "state": "unknown",
                    "reason": None,
                    "counts": {"sent": 0, "received": 0, "not_delivered": 0},
                    "counterparties": [],
                    "latest": None,
                    "previous": None,
                    "ticks": [],
                    "ticks_truncated": False,
                    "conversation_supported": True,
                }
            for row in rows:
                participants = [self._actor(row[c], actors) for c in ("sender", "recipient")]
                in_window = row["last_event_at"] >= since or row["ended_at"] is None
                delivered = bool(row["delivered"])
                for a in participants:
                    identity = a["id"]
                    if a["kind"] != "agent" or not identity or (agent_id and agent_id != identity):
                        continue
                    if identity not in summaries:
                        summaries[identity] = {
                            "agent": a,
                            "runtime": "unknown",
                            "state": "offline",
                            "reason": None,
                            "counts": {"sent": 0, "received": 0, "not_delivered": 0},
                            "counterparties": [],
                            "latest": None,
                            "previous": None,
                            "ticks": [],
                            "ticks_truncated": False,
                            "conversation_supported": True,
                        }
                for identity in {a["id"] for a in participants if a["kind"] == "agent"}:
                    if identity not in summaries:
                        continue
                    item = summaries[identity]
                    if not in_window:
                        if (
                            item["previous"] is None
                            or item["previous"]["time"] < row["last_event_at"]
                        ):
                            item["previous"] = {"id": row["id"], "time": row["last_event_at"]}
                        continue
                    if item["latest"] is None or item["latest"]["time"] < row["last_event_at"]:
                        item["latest"] = {"id": row["id"], "time": row["last_event_at"]}
                    sent = participants[0]["id"] == identity
                    received = participants[1]["id"] == identity
                    item["counts"]["sent"] += int(sent)
                    item["counts"]["received"] += int(received and delivered)
                    item["counts"]["not_delivered"] += int(not delivered)
                    for other in participants:
                        if other["id"] != identity and other not in item["counterparties"]:
                            item["counterparties"].append(other)
                    for direction, yes in (("sent", sent), ("received", received)):
                        if not yes:
                            continue
                        if len(item["ticks"]) >= 200:
                            item["ticks_truncated"] = True
                        else:
                            item["ticks"].append(
                                {
                                    "interaction_id": row["id"],
                                    "direction": direction,
                                    "time": row["accepted_at"],
                                    "time_basis": "recorded",
                                    "delivered": delivered,
                                }
                            )
            result = sorted(
                summaries.values(),
                key=lambda a: (
                    a["latest"] is not None,
                    (a["latest"] or {}).get("time", ""),
                    a["agent"]["name"],
                ),
                reverse=True,
            )
            return {
                "schema_version": 1,
                "items": result,
                "snapshot": token,
                "coverage": await self._coverage(conn, since, until, snap, truncated=truncated),
                "conversation_supported": True,
            }

    async def prune(self, retention_days=30):
        async with self._tx() as conn:
            cutoff = cutoff_iso(days=retention_days)
            anchored = exists(
                select(_C.c.id).where(_C.c.kind == "interaction", _C.c.artifact_id == _I.c.id)
            )
            result = await conn.execute(
                update(_I)
                .where(_I.c.accepted_at < cutoff, ~anchored, _I.c.body.is_not(None))
                .values(body=None)
            )
            await conn.execute(
                update(_COVER).where(_COVER.c.retained_from < cutoff).values(retained_from=cutoff)
            )
            return result.rowcount

    async def attribution(self, sender, registration=None, owner_origin=False):
        from agent_backbone.services.database._interaction_capture import actor, owner

        async with self._tx() as conn:
            return (
                owner()
                if owner_origin
                else await actor(
                    conn,
                    sender,
                    registration=registration,
                    attribution="credential_bound" if registration else "unverified",
                )
            )

    async def validate_relations(self, relations, sender_id, *, operator=False):
        from agent_backbone.services.database.models import ReportORM

        reports = ReportORM.__table__
        if relations and not operator and not sender_id:
            raise ValueError("Recorded relations require a registration-bound sender")
        async with self._tx() as conn:
            result = []
            for relation in relations:
                value = dict(relation)
                target = value["target"]
                kind = target["kind"]
                type_ = value["type"]
                if (
                    (type_ == "reply_to" and kind != "interaction")
                    or (type_ == "corrects" and kind not in ("interaction", "report"))
                    or (type_ == "for_owner_request" and kind != "owner_request")
                    or (type_ == "asked_in_conversation" and kind != "conversation")
                    or (type_ == "from_report" and kind != "report")
                ):
                    raise ValueError("Relation type and target kind do not match")
                valid = False
                if kind in ("interaction", "owner_request"):
                    row = (
                        (await conn.execute(select(_I).where(_I.c.id == target["id"])))
                        .mappings()
                        .first()
                    )
                    valid = bool(
                        row
                        and (
                            operator
                            or any(
                                a.get("kind") == "agent"
                                and a.get("id") == sender_id
                                and a.get("attribution") != "unverified"
                                for a in (json.loads(row["sender"]), json.loads(row["recipient"]))
                            )
                        )
                    )
                    if kind == "owner_request" and row:
                        valid = valid and json.loads(row["sender"])["kind"] == "owner"
                elif kind == "report":
                    try:
                        number = int(target["id"])
                    except ValueError:
                        number = -1
                    if not 0 <= number < 2**31:  # beyond reports.id (int4 on PostgreSQL): absent
                        number = -1
                    row = (
                        (await conn.execute(select(reports).where(reports.c.id == number)))
                        .mappings()
                        .first()
                    )
                    valid = bool(row and (operator or row["author_id"] == sender_id))
                    if valid:
                        if target.get("artifact_kind") not in (None, row["kind"]):
                            raise ValueError("Report relation kind does not match its record")
                        value["target"] = target | {"artifact_kind": row["kind"]}
                elif kind == "conversation":
                    query = select(_C).where(
                        _C.c.kind == target.get("artifact_kind"), _C.c.artifact_id == target["id"]
                    )
                    if target.get("entry_id") is not None:
                        if not 0 < target["entry_id"] < 2**63:  # zero or past an int8 id: absent
                            raise ValueError("Relation target is absent or not accessible")
                        query = query.where(_C.c.id == target["entry_id"])
                    if not operator:
                        query = query.where(
                            or_(_C.c.author_id == sender_id, _C.c.recipient_id == sender_id)
                        )
                    valid = bool((await conn.execute(query.limit(1))).first())
                if not valid:
                    raise ValueError("Relation target is absent or not accessible")
                result.append(value | {"recorded_at": now_iso(), "provenance": "explicit_sender"})
            return result

    async def observe(self, operation_id, kind, **kwargs):
        from agent_backbone.services.database._interaction_capture import event

        async with self._tx() as conn:
            await event(conn, operation_id, kind, **kwargs)

    async def checkpoint(self, recorder_id):
        async with self._tx() as conn:
            await observations.checkpoint(conn, recorder_id)

    async def state_spans(self, *, agent_id, since, until, limit=200, cursor=None, snapshot=None):
        async with self._tx() as conn:
            after = None
            if cursor:
                data = self._decode(cursor)
                if data.get("kind") != "states" or data.get("scope") != [agent_id, since, until]:
                    raise ValueError("State cursor belongs to different filters")
                if snapshot and snapshot != data["snapshot"]:
                    raise ValueError("Snapshot mismatch")
                snapshot, after = data["snapshot"], data["after"]
            snap, token = await self._snapshot(conn, since, until, snapshot)
            items, coverage, next_after = await observations.state_spans(
                conn, agent_id, since, until, snap.get("state_high", 0), after=after, limit=limit
            )
            return {
                "schema_version": 1,
                "items": items,
                "coverage": coverage,
                "snapshot": token,
                "next_cursor": self._encode(
                    {
                        "kind": "states",
                        "scope": [agent_id, since, until],
                        "snapshot": token,
                        "after": next_after,
                    }
                )
                if next_after
                else None,
            }
