"""Reconciliation is read-only and cannot turn a reservation into activation."""

import asyncio
import threading
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from sqlalchemy import text

from agent_backbone.config import sqlite_url
from agent_backbone.services.database import (
    BackboneDB,
    ProfileIdentity,
    ProfileRegistry,
    ProfileReservation,
    check_profile_startup,
    reconcile_profile_provider,
)


@pytest.fixture
async def prepared(tmp_path):
    root = tmp_path / "provider a"
    root.mkdir(mode=0o700)
    database_path = root / "backbone.db"
    async with BackboneDB.connect(sqlite_url(root)) as db:
        identity = ProfileIdentity("profile-a", await db.signing.audience(), "lifecycle-a1")
        await db.profile_identity.reserve(identity)
        registry = ProfileRegistry.create(tmp_path / "control" / "registry.db")
        reservation = registry.reserve(ProfileReservation(identity, root, "Work", "baseline-v1"))
        yield registry, reservation, database_path, root / "tmux" / "server.sock", db


async def test_reconcile_captures_identity_without_writes_or_activation(prepared):
    registry, reservation, path, socket, db = prepared
    before = path.read_bytes(), registry.path.read_bytes()
    provider = await reconcile_profile_provider(registry, reservation, path, socket)
    assert provider.identity_response() == {
        "schema_version": 1,
        **vars(reservation.identity),
        "baseline_version": "baseline-v1",
        "tmux": {"socket_path": str(socket)},
    }
    with pytest.raises(FrozenInstanceError):
        provider.socket_path = Path("/other")
    assert not socket.parent.exists()
    assert registry.show("profile-a")["binding"] is None
    assert registry.show("profile-a")["state"] == "creating"
    assert before == (path.read_bytes(), registry.path.read_bytes())
    with pytest.raises(ValueError, match="not ready"):
        await check_profile_startup(db.url, reservation.data_dir)


@pytest.mark.parametrize("cancel", [False, True])
async def test_admission_wait_keeps_loop_responsive_and_releases_after_cancellation(
    prepared, monkeypatch, cancel
):
    registry, reservation, path, socket, _ = prepared
    provider = await reconcile_profile_provider(registry, reservation, path, socket)
    acquired, finish_acquiring, released = (threading.Event() for _ in range(3))
    entered = False
    original = ProfileRegistry.operation

    @contextmanager
    def delayed(self, *args):
        with original(self, *args):
            acquired.set()
            assert finish_acquiring.wait(5)
            try:
                yield
            finally:
                released.set()

    monkeypatch.setattr(ProfileRegistry, "operation", delayed)

    async def serve():
        nonlocal entered
        async with provider.operation():
            entered = True

    request = asyncio.create_task(serve())
    try:
        assert await asyncio.to_thread(acquired.wait, 3)
        assert not request.done()
        if cancel:
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        finish_acquiring.set()
        if not cancel:
            await request
        assert await asyncio.to_thread(released.wait, 3)
        assert entered is not cancel
    finally:
        finish_acquiring.set()
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
    registry.invalidate(reservation.identity, reservation.data_dir)


@pytest.mark.parametrize("admission", [False, True])
async def test_cancelled_registry_workers_keep_their_slots(prepared, monkeypatch, admission):
    registry, reservation, path, socket, _ = prepared
    provider = await reconcile_profile_provider(registry, reservation, path, socket)
    loop = asyncio.get_running_loop()
    saturated, extra_started = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    counter_lock = threading.Lock()
    started = 0
    original = ProfileRegistry.check_reservation

    def delayed(self, *args):
        nonlocal started
        with counter_lock:
            started += 1
            if started == 4:
                loop.call_soon_threadsafe(saturated.set)
            elif started > 4:
                loop.call_soon_threadsafe(extra_started.set)
        assert release.wait(5)
        return original(self, *args)

    monkeypatch.setattr(ProfileRegistry, "check_reservation", delayed)

    async def call():
        if admission:
            async with provider.operation():
                pass
        else:
            await provider.check()

    requests = [asyncio.create_task(call()) for _ in range(12)]
    later = None
    try:
        await asyncio.wait_for(saturated.wait(), 3)
        for task in requests:
            task.cancel()
        await asyncio.gather(*requests, return_exceptions=True)
        later = asyncio.create_task(call())
        # Cancelled coroutines cannot admit more workers behind the stalled four.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(extra_started.wait(), 0.05)
        release.set()
        await asyncio.wait_for(later, 3)
        await asyncio.wait_for(extra_started.wait(), 3)
    finally:
        release.set()
        for task in [*requests, *([later] if later else [])]:
            task.cancel()
        await asyncio.gather(*requests, *([later] if later else []), return_exceptions=True)
    # Late admission completion has also released every lock.
    registry.invalidate(reservation.identity, reservation.data_dir)


@pytest.mark.parametrize("change", ["invalidate", "identity", "audience", "missing", "baseline"])
async def test_recheck_refuses_replacement_or_restore(prepared, change):
    registry, reservation, path, socket, db = prepared
    provider = await reconcile_profile_provider(registry, reservation, path, socket)
    if change == "invalidate":
        registry.invalidate(reservation.identity, reservation.data_dir)
    elif change == "baseline":
        provider = replace(provider, reservation=replace(reservation, baseline_version="other"))
    else:
        async with db.engine.begin() as conn:
            await conn.execute(
                text(
                    {
                        "identity": "UPDATE profile_identity SET lifecycle_id = 'old-lifecycle'",
                        "audience": "UPDATE signing_install SET audience = 'other-installation'",
                        "missing": "DELETE FROM profile_identity",
                    }[change]
                )
            )
    with pytest.raises(ValueError):
        await provider.check()


@pytest.mark.parametrize("kind", ["outside", "symlink", "missing", "control"])
async def test_unowned_or_missing_database_is_not_opened_for_writes(prepared, tmp_path, kind):
    registry, reservation, path, socket, _ = prepared
    if kind == "outside":
        path = tmp_path / "other.db"
    elif kind == "symlink":
        link = path.parent / "alias.db"
        link.symlink_to(path)
        path = link
    elif kind == "missing":
        path = path.parent / "missing.db"
    else:
        socket = path.parent / "bad\nserver.sock"
    from sqlalchemy.exc import OperationalError

    with pytest.raises((ValueError, OperationalError)):
        await reconcile_profile_provider(registry, reservation, path, socket)
    if kind in {"outside", "missing"}:
        assert not path.exists()
