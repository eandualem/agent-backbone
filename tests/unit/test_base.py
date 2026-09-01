"""Tests for agent_backbone.base — the lifecycle protocol and manager."""

from __future__ import annotations

import pytest

from agent_backbone.base import LifecycleAware, LifecycleManager


class _Component:
    def __init__(self, name: str, log: list[str], fail_start: bool = False) -> None:
        self.name, self.log, self.fail_start = name, log, fail_start
        self.healthy = True

    async def start(self) -> None:
        if self.fail_start:
            raise RuntimeError(f"{self.name} boom")
        self.log.append(f"start:{self.name}")

    async def stop(self) -> None:
        self.log.append(f"stop:{self.name}")

    async def health_check(self) -> dict:
        return {"healthy": self.healthy}


class _NotAComponent:
    async def start(self) -> None:
        pass


class TestProtocol:
    def test_runtime_checkable(self):
        assert isinstance(_Component("a", []), LifecycleAware)
        assert not isinstance(_NotAComponent(), LifecycleAware)


class TestLifecycleManager:
    async def test_starts_in_order_and_stops_in_reverse(self):
        log: list[str] = []
        manager = LifecycleManager()
        manager.register("a", _Component("a", log))
        manager.register("b", _Component("b", log))
        await manager.start_all()
        await manager.stop_all()
        assert log == ["start:a", "start:b", "stop:b", "stop:a"]

    async def test_rolls_back_on_start_failure(self):
        log: list[str] = []
        manager = LifecycleManager()
        manager.register("a", _Component("a", log))
        manager.register("b", _Component("b", log, fail_start=True))
        with pytest.raises(RuntimeError, match="b boom"):
            await manager.start_all()
        assert log == ["start:a", "stop:a"]

    def test_duplicate_name_rejected(self):
        manager = LifecycleManager()
        manager.register("a", _Component("a", []))
        with pytest.raises(ValueError, match="already registered"):
            manager.register("a", _Component("a", []))

    async def test_health_aggregates(self):
        manager = LifecycleManager()
        good, bad = _Component("good", []), _Component("bad", [])
        bad.healthy = False
        manager.register("good", good)
        manager.register("bad", bad)
        health = await manager.health()
        assert health["healthy"] is False
        assert health["components"] == {"good": {"healthy": True}, "bad": {"healthy": False}}

    async def test_health_check_exception_is_unhealthy(self):
        class _Broken(_Component):
            async def health_check(self) -> dict:
                raise RuntimeError("down")

        manager = LifecycleManager()
        manager.register("x", _Broken("x", []))
        health = await manager.health()
        assert health["components"]["x"] == {"healthy": False, "error": "down"}
