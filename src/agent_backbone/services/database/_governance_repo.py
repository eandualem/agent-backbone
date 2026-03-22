"""Governance track persistence — CRUD operations for tracks and instances."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agent_backbone.services.database.models import (
    GovernanceTrackInstanceORM,
    GovernanceTrackLayoutORM,
    GovernanceTrackORM,
)

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class GovernanceRepo:
    """Query repository for governance tracks and instances."""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    async def list_tracks(self) -> list[dict]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(GovernanceTrackORM).order_by(GovernanceTrackORM.name)
            )
            rows = result.scalars().all()
            return [
                {
                    "id": r.id,
                    "name": r.name,
                    "description": r.description,
                    "definition": json.loads(r.definition),
                    "created_at": r.created_at,
                    "updated_at": r.updated_at,
                }
                for r in rows
            ]

    async def get_track(self, track_id: str) -> dict | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(GovernanceTrackORM).where(GovernanceTrackORM.id == track_id)
            )
            r = result.scalar_one_or_none()
            if r is None:
                return None
            return {
                "id": r.id,
                "name": r.name,
                "description": r.description,
                "definition": json.loads(r.definition),
                "created_at": r.created_at,
                "updated_at": r.updated_at,
            }

    async def create_track(
        self, track_id: str, name: str, description: str, definition: dict
    ) -> dict:
        now = _now()
        async with self._session_factory() as session:
            row = GovernanceTrackORM(
                id=track_id,
                name=name,
                description=description,
                definition=json.dumps(definition),
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            await session.commit()
            return {
                "id": track_id,
                "name": name,
                "description": description,
                "definition": definition,
                "created_at": now,
                "updated_at": now,
            }

    async def update_track(
        self,
        track_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        definition: dict | None = None,
    ) -> dict | None:
        now = _now()
        values: dict = {"updated_at": now}
        if name is not None:
            values["name"] = name
        if description is not None:
            values["description"] = description
        if definition is not None:
            values["definition"] = json.dumps(definition)

        async with self._session_factory() as session:
            result = await session.execute(
                update(GovernanceTrackORM)
                .where(GovernanceTrackORM.id == track_id)
                .values(**values)
            )
            await session.commit()
            if result.rowcount == 0:
                return None
        return await self.get_track(track_id)

    async def delete_track(self, track_id: str) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(GovernanceTrackORM).where(GovernanceTrackORM.id == track_id)
            )
            await session.commit()
            return result.rowcount > 0

    async def list_instances(self, track_id: str) -> list[dict]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(GovernanceTrackInstanceORM)
                .where(GovernanceTrackInstanceORM.track_id == track_id)
                .order_by(GovernanceTrackInstanceORM.created_at.desc())
            )
            rows = result.scalars().all()
            return [
                {
                    "id": r.id,
                    "track_id": r.track_id,
                    "context": json.loads(r.context),
                    "current_state": r.current_state,
                    "history": json.loads(r.history),
                    "created_at": r.created_at,
                    "updated_at": r.updated_at,
                }
                for r in rows
            ]

    async def create_instance(
        self,
        instance_id: str,
        track_id: str,
        context: dict,
        current_state: str,
    ) -> dict:
        now = _now()
        async with self._session_factory() as session:
            row = GovernanceTrackInstanceORM(
                id=instance_id,
                track_id=track_id,
                context=json.dumps(context),
                current_state=current_state,
                history=json.dumps([]),
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            await session.commit()
            return {
                "id": instance_id,
                "track_id": track_id,
                "context": context,
                "current_state": current_state,
                "history": [],
                "created_at": now,
                "updated_at": now,
            }

    async def update_instance(
        self,
        instance_id: str,
        *,
        current_state: str | None = None,
        context: dict | None = None,
        history: list | None = None,
    ) -> dict | None:
        now = _now()
        values: dict = {"updated_at": now}
        if current_state is not None:
            values["current_state"] = current_state
        if context is not None:
            values["context"] = json.dumps(context)
        if history is not None:
            values["history"] = json.dumps(history)

        async with self._session_factory() as session:
            result = await session.execute(
                update(GovernanceTrackInstanceORM)
                .where(GovernanceTrackInstanceORM.id == instance_id)
                .values(**values)
            )
            await session.commit()
            if result.rowcount == 0:
                return None

        async with self._session_factory() as session:
            result = await session.execute(
                select(GovernanceTrackInstanceORM)
                .where(GovernanceTrackInstanceORM.id == instance_id)
            )
            r = result.scalar_one_or_none()
            if r is None:
                return None
            return {
                "id": r.id,
                "track_id": r.track_id,
                "context": json.loads(r.context),
                "current_state": r.current_state,
                "history": json.loads(r.history),
                "created_at": r.created_at,
                "updated_at": r.updated_at,
            }

    async def get_instance(self, instance_id: str) -> dict | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(GovernanceTrackInstanceORM)
                .where(GovernanceTrackInstanceORM.id == instance_id)
            )
            r = result.scalar_one_or_none()
            if r is None:
                return None
            return {
                "id": r.id,
                "track_id": r.track_id,
                "context": json.loads(r.context),
                "current_state": r.current_state,
                "history": json.loads(r.history),
                "created_at": r.created_at,
                "updated_at": r.updated_at,
            }

    # --- Layouts ---

    async def get_layout(self, track_id: str) -> dict | None:
        """Get layout positions for a track."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(GovernanceTrackLayoutORM).where(
                    GovernanceTrackLayoutORM.track_id == track_id
                )
            )
            r = result.scalar_one_or_none()
            if r is None:
                return None
            return {
                "track_id": r.track_id,
                "positions": json.loads(r.positions),
                "updated_at": r.updated_at,
            }

    async def upsert_layout(self, track_id: str, positions: dict) -> dict:
        """Create or update layout positions for a track."""
        now = _now()
        async with self._session_factory() as session:
            result = await session.execute(
                select(GovernanceTrackLayoutORM).where(
                    GovernanceTrackLayoutORM.track_id == track_id
                )
            )
            existing = result.scalar_one_or_none()
            if existing is not None:
                existing.positions = json.dumps(positions)
                existing.updated_at = now
            else:
                session.add(
                    GovernanceTrackLayoutORM(
                        track_id=track_id,
                        positions=json.dumps(positions),
                        updated_at=now,
                    )
                )
            await session.commit()
        return {
            "track_id": track_id,
            "positions": positions,
            "updated_at": now,
        }
