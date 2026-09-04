"""The running backbone restarts onto new code, once, when nothing is being routed."""

from __future__ import annotations

from unittest.mock import AsyncMock

from agent_backbone.release import Installation
from agent_backbone.services.jobs import UpgradeWatch


def _watch(identities: list[str], *, enabled=True, in_flight=0):
    seen = iter(identities)
    restart = AsyncMock()
    watch = UpgradeWatch(
        enabled=lambda: enabled,
        restart=restart,
        in_flight=lambda: in_flight,
        identity=lambda install: next(seen),
        install=Installation("uv"),
    )
    return watch, restart


async def test_unchanged_code_does_nothing():
    watch, restart = _watch(["version:1", "version:1"])
    assert await watch.run() == {"code": "version:1"}
    restart.assert_not_called()


async def test_changed_code_restarts_once():
    watch, restart = _watch(["version:1", "version:2", "version:2"])
    first = await watch.run()
    assert first["restart"] == "requested" and first["changed_from"] == "version:1"
    assert await watch.run() == {"restart": "requested"}
    restart.assert_awaited_once()


async def test_disabled_reports_but_does_not_restart():
    watch, restart = _watch(["git:a", "git:b"], enabled=False)
    assert (await watch.run())["restart"] == "disabled"
    restart.assert_not_called()


async def test_routing_in_flight_defers_the_restart():
    watch, restart = _watch(["git:a", "git:b", "git:b"], in_flight=2)
    assert (await watch.run())["restart"] == "deferred (2 in flight)"
    restart.assert_not_called()
    assert not watch.requested
