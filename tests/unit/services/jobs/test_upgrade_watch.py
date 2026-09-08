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


async def test_a_checkout_on_another_branch_is_development_not_an_upgrade():
    watch, restart = _watch(["git:develop@a", "git:feat/x@b", "git:develop@c"])
    assert (await watch.run())["restart"] == "other branch"
    restart.assert_not_called()
    # back on the branch it started on, with new commits: that is the upgrade
    assert (await watch.run())["restart"] == "requested"
    restart.assert_awaited_once()


async def test_disabled_reports_but_does_not_restart():
    watch, restart = _watch(["git:develop@a", "git:develop@b"], enabled=False)
    assert (await watch.run())["restart"] == "disabled"
    restart.assert_not_called()


async def test_routing_in_flight_defers_the_restart():
    watch, restart = _watch(["git:develop@a", "git:develop@b", "git:develop@b"], in_flight=2)
    assert (await watch.run())["restart"] == "deferred (2 in flight)"
    restart.assert_not_called()
    assert not watch.requested


async def test_holds_are_operation_scoped_and_reset_on_manual_restart():
    watch, restart = _watch(["version:1", "version:2", "version:2"])
    watch.set_hold("first", True)
    watch.set_hold("second", True)
    assert watch.set_hold("first", False) == {"held": True, "operation_held": False}
    assert (await watch.run())["restart"] == "held"
    watch.set_hold("second", False)
    assert (await watch.run())["restart"] == "requested"
    restart.assert_awaited_once()
    fresh, _ = _watch(["version:2", "version:2"])
    assert await fresh.run() == {"code": "version:2"}


async def test_a_requested_restart_cannot_be_promised_a_hold():
    import pytest

    watch, _ = _watch(["version:1", "version:2"])
    await watch.run()
    with pytest.raises(ValueError, match="already requested"):
        watch.set_hold("late", True)
