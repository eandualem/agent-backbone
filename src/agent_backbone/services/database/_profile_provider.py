"""Read-only reconciliation for a future bound provider startup.

This does not publish readiness or authorize activation. The normal startup
guard continues to refuse reserved providers until their isolation is complete.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from agent_backbone.services.database._profile_identity_repo import _read
from agent_backbone.services.database._profile_registry import (
    ProfileRegistry,
    ProfileReservation,
)


@dataclass(frozen=True)
class ProfileProvider:
    """Captured provider metadata; never inferred from a client or display name.

    The activation caller must separately establish runtime/integration isolation
    and pin the terminal server before installing this context in a serving app.
    No production startup caller installs it yet.
    """

    reservation: ProfileReservation
    registry_path: Path
    database_path: Path
    socket_path: Path

    async def check(self) -> None:
        """Recheck external ownership and local identity without creating storage.

        An invalidation or restored database fails closed on subsequent checks.
        This is not a replacement protocol: the lifecycle owner must fence jobs,
        streams and in-flight work before replacing either store.
        """
        root = self.reservation.data_dir
        if self.database_path == self.socket_path:
            raise ValueError("Provider database and terminal socket must be distinct")
        for path in (self.database_path, self.socket_path):
            if (
                not path.is_absolute()
                or path.resolve() != path
                or path == root
                or not path.is_relative_to(root)
                or any(ord(c) < 0x20 or ord(c) == 0x7F for c in str(path))
            ):
                raise ValueError("Provider path does not match its reserved directory")
        registry = ProfileRegistry(self.registry_path)

        def check_reservation():
            if registry.check_reservation(self.reservation.identity, root) != self.reservation:
                raise ValueError("Provider reservation metadata does not match")

        await asyncio.to_thread(check_reservation)
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{self.database_path.as_uri()}?mode=ro&uri=true"
        )
        try:
            async with engine.connect() as conn:
                await conn.execute(text("BEGIN"))
                exists = await conn.run_sync(lambda c: inspect(c).has_table("profile_identity"))
                if not exists or await _read(conn) != self.reservation.identity:
                    raise ValueError("Provider identity does not match its reservation")
        finally:
            await engine.dispose()
        await asyncio.to_thread(check_reservation)

    def identity_response(self) -> dict:
        identity = self.reservation.identity
        return {
            "schema_version": 1,
            "profile_id": identity.profile_id,
            "installation_id": identity.installation_id,
            "lifecycle_id": identity.lifecycle_id,
            "baseline_version": self.reservation.baseline_version,
            "tmux": {"socket_path": str(self.socket_path)},
        }


async def reconcile_profile_provider(
    registry: ProfileRegistry,
    reservation: ProfileReservation,
    database_path: Path,
    socket_path: Path,
) -> ProfileProvider:
    """Capture matching metadata, leaving discovery non-ready and files untouched."""
    provider = ProfileProvider(reservation, registry.path, database_path, socket_path)
    await provider.check()
    return provider
