"""Atomic usage ingestion: request revisions and file checkpoint commit together."""

from __future__ import annotations

import json

from sqlalchemy import bindparam, insert, select, update

from agent_backbone.services.database._repo import Repo
from agent_backbone.services.database.models import UsageEventORM, UsageSessionORM
from agent_backbone.usage import Price, UsageEvent, estimate, timestamp, usage_id

_S = UsageSessionORM.__table__
_E = UsageEventORM.__table__
_BATCH_SIZE = 500


def _session(row) -> dict:
    result = dict(row)
    result["cursor"] = json.loads(result["cursor"])
    result["launches"] = json.loads(result["launches"])
    return result


class UsageRepo(Repo):
    async def session(self, identity: str) -> dict:
        """Read one conversation by its indexed identity, including newly remembered children."""
        async with self._tx() as conn:
            row = (await conn.execute(select(_S).where(_S.c.id == identity))).mappings().one()
            return _session(row)

    async def sessions(self) -> list[dict]:
        async with self._tx() as conn:
            return [_session(r) for r in (await conn.execute(select(_S))).mappings()]

    async def remember(
        self,
        agent: str,
        runtime: str,
        session_id: str,
        *,
        at: str,
        parent_id: str | None = None,
        launch_id: str | None = None,
    ) -> str:
        identity = usage_id(runtime, session_id)
        async with self._tx() as conn:
            # Portable upsert for the SQLite/PostgreSQL engines we support.
            from sqlalchemy import text

            await conn.execute(
                text("""INSERT INTO usage_sessions
                (id, agent_name, runtime, session_id, parent_id, first_seen, last_seen)
                VALUES (:id, :agent, :runtime, :session_id, :parent, :at, :at)
                ON CONFLICT(id) DO NOTHING"""),
                {
                    "id": identity,
                    "agent": agent,
                    "runtime": runtime,
                    "session_id": session_id,
                    "parent": parent_id,
                    "at": timestamp(at),
                },
            )
            row = (await conn.execute(select(_S).where(_S.c.id == identity))).mappings().one()
            if row["agent_name"] != agent:
                raise ValueError("runtime session already attributed to another agent")
            if parent_id and row["parent_id"] != parent_id:
                if row["parent_id"] is not None or parent_id == identity:
                    raise ValueError("runtime session has conflicting parent lineage")
                await conn.execute(
                    update(_S)
                    .where(_S.c.id == identity, _S.c.parent_id.is_(None))
                    .values(parent_id=parent_id)
                )
                parent = await conn.scalar(select(_S.c.parent_id).where(_S.c.id == identity))
                if parent != parent_id:
                    raise ValueError("runtime session has conflicting parent lineage")
            launches = json.loads(row["launches"])
            if launch_id and launch_id not in launches:
                launches.append(launch_id)
                await conn.execute(
                    update(_S).where(_S.c.id == identity).values(launches=json.dumps(launches))
                )
        return identity

    async def ingest(
        self,
        session: dict,
        *,
        path: str,
        offset: int,
        cursor: dict,
        events: list[UsageEvent],
        coverage: str,
        detail: str,
        prices: dict,
        observed_at: str,
    ) -> bool:
        async with self._tx() as conn:
            # Compare-and-swap protects against a monitor and a CLI refresh using
            # the same old checkpoint. A losing reader simply retries next time.
            result = await conn.execute(
                update(_S)
                .where(
                    _S.c.id == session["id"],
                    _S.c.offset == session["offset"],
                    _S.c.cursor == json.dumps(session["cursor"], sort_keys=True),
                )
                .values(
                    source_path=path,
                    offset=offset,
                    cursor=json.dumps(cursor, sort_keys=True),
                    coverage=coverage,
                    detail=detail,
                    last_seen=observed_at,
                )
            )
            if result.rowcount != 1:
                return False
            keys = list({event.key for event in events})
            existing = {}
            for start in range(0, len(keys), _BATCH_SIZE):
                rows = await conn.execute(
                    select(_E).where(
                        _E.c.session == session["id"],
                        _E.c.event_key.in_(keys[start : start + _BATCH_SIZE]),
                    )
                )
                existing.update((row["event_key"], dict(row)) for row in rows.mappings())
            original_keys = set(existing)
            changed = {}
            for event in events:
                event = UsageEvent.model_validate(event.model_dump())
                old = existing.get(event.key)
                selected_prices = prices
                if old:
                    previous = UsageEvent.model_validate_json(old["data"])
                    if (event.revision_at or event.at) < (previous.revision_at or previous.at):
                        continue
                    event = event.model_copy(
                        update={
                            "revision_at": event.revision_at or event.at,
                            "at": previous.at,
                            "turn_id": previous.turn_id or event.turn_id,
                        }
                    )
                    old_cost = json.loads(old["cost"])
                    if old_cost.get("basis") and previous.model == event.model:
                        basis = {
                            k: v for k, v in old_cost["basis"].items() if k in Price.model_fields
                        }
                        selected_prices = {event.model: basis}
                cost = estimate(event, selected_prices)
                values = {
                    "at": event.at,
                    "model": event.model,
                    "data": event.model_dump_json(),
                    "cost": json.dumps(cost),
                }
                if old is None or any(old[key] != value for key, value in values.items()):
                    existing[event.key] = values
                    changed[event.key] = values
            new = [
                {"session": session["id"], "event_key": key, **values}
                for key, values in changed.items()
                if key not in original_keys
            ]
            revised = [
                {"_key": key, **values} for key, values in changed.items() if key in original_keys
            ]
            if new:
                await conn.execute(insert(_E), new)
            if revised:
                await conn.execute(
                    update(_E)
                    .where(_E.c.session == session["id"], _E.c.event_key == bindparam("_key"))
                    .values({key: bindparam(key) for key in ("at", "model", "data", "cost")}),
                    revised,
                )
        return True

    async def event_batches(
        self,
        *,
        agent: str | None = None,
        runtime: str | None = None,
        session: str | None = None,
        sessions: set[str] | None = None,
        since: str | None = None,
        until: str | None = None,
    ):
        """Stream one ordered selection in bounded batches under one read transaction."""
        if sessions is not None and not sessions:
            return
        statement = select(_E).join(_S, _S.c.id == _E.c.session)
        if agent:
            statement = statement.where(_S.c.agent_name == agent)
        if runtime:
            statement = statement.where(_S.c.runtime == runtime)
        if session:
            statement = statement.where(_S.c.id == session)
        if sessions is not None:
            statement = statement.where(_S.c.id.in_(sessions))
        if since:
            statement = statement.where(_E.c.at >= since)
        if until:
            statement = statement.where(_E.c.at < until)
        async with (
            self._tx() as conn,
            conn.stream(statement.order_by(_E.c.at, _E.c.event_key)) as result,
        ):
            async for batch in result.mappings().partitions(_BATCH_SIZE):
                yield batch

    async def events(self, **filters) -> list[dict]:
        return [
            {"session": r["session"], **json.loads(r["data"]), "cost": json.loads(r["cost"])}
            async for batch in self.event_batches(**filters)
            for r in batch
        ]

    async def observed(self, identities: list[str], at: str) -> None:
        """Refresh collection timestamps without rewriting unchanged checkpoints."""
        if not identities:
            return
        async with self._tx() as conn:
            for start in range(0, len(identities), _BATCH_SIZE):
                await conn.execute(
                    update(_S)
                    .where(
                        _S.c.id.in_(identities[start : start + _BATCH_SIZE]), _S.c.last_seen < at
                    )
                    .values(last_seen=at)
                )
