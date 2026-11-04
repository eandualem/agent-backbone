"""Read-only reconciliation for a future bound provider startup.

This does not publish readiness or authorize activation. The normal startup
guard continues to refuse reserved providers until their isolation is complete.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
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
    _registry_slots: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(4), compare=False, repr=False
    )

    async def _registry_call(self, call):
        # Cancellation of the caller must not free a slot while SQLite recovery
        # or a writer still occupies its worker. Keep the executor queue bounded.
        await self._registry_slots.acquire()
        try:
            future = asyncio.get_running_loop().run_in_executor(None, call)
        except BaseException:
            self._registry_slots.release()
            raise

        def finished(done):
            self._registry_slots.release()
            if not done.cancelled():
                done.exception()  # Observe failures even if the caller left.

        future.add_done_callback(finished)
        return future

    @asynccontextmanager
    async def operation(self):
        """Retain this reservation until a guarded operation finishes or cancels."""
        operation = ProfileRegistry(self.registry_path).operation(
            self.reservation.identity, self.reservation.data_dir
        )
        # Registry reads can wait on SQLite recovery/writers. Keep that wait off
        # the event loop, without abandoning a lock acquired after cancellation.
        acquisition = await self._registry_call(operation.__enter__)
        try:
            await asyncio.shield(acquisition)
        except BaseException:

            def release_when_acquired(done):
                if not done.cancelled() and done.exception() is None:
                    operation.__exit__(None, None, None)

            acquisition.add_done_callback(release_when_acquired)
            raise
        try:
            yield
        finally:
            operation.__exit__(None, None, None)

    async def check(self) -> None:
        """Recheck external ownership and local identity without creating storage.

        An invalidation or restored database fails closed on subsequent checks.
        This is not a replacement protocol: the lifecycle owner must fence jobs,
        streams and in-flight work before replacing either store.
        """
        root = self.reservation.data_dir
        if self.database_path == self.socket_path:
            raise ValueError("Provider database and terminal socket must be distinct")
        # An adopted installation keeps the tmux server it already uses.
        adopted = self.reservation.kind == "legacy_default"
        for path in (self.database_path,) if adopted else (self.database_path, self.socket_path):
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

        await asyncio.shield(await self._registry_call(check_reservation))
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
        await asyncio.shield(await self._registry_call(check_reservation))

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
