"""Tests for agent_backbone.base — protocols, lifecycle, exceptions."""

from __future__ import annotations

import pytest

from agent_backbone.base import (
    BackboneError,
    ConfigurationError,
    DeliveryError,
    ExternalServiceError,
    LifecycleAware,
    LifecycleManager,
    StateError,
)

# ── Protocols ──────────────────────────────────────────────


class _GoodComponent:
    """A component that satisfies LifecycleAware."""

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def health_check(self) -> dict:
        return {"healthy": True}


class _BadComponent:
    """A component missing health_check — not LifecycleAware."""

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class TestLifecycleAwareProtocol:
    def test_satisfies_protocol(self):
        assert isinstance(_GoodComponent(), LifecycleAware)

    def test_missing_method_fails(self):
        assert not isinstance(_BadComponent(), LifecycleAware)


# ── Exceptions ─────────────────────────────────────────────


class TestExceptionHierarchy:
    def test_backbone_error_is_exception(self):
        err = BackboneError("test")
        assert isinstance(err, Exception)
        assert str(err) == "test"
        assert err.message == "test"

    def test_backbone_error_defaults(self):
        err = BackboneError("x")
        assert err.category == "general"
        assert err.severity == "medium"
        assert err.retry_allowed is False

    def test_backbone_error_kwargs(self):
        err = BackboneError("x", context="extra")
        assert err.context == "extra"

    def test_configuration_error(self):
        err = ConfigurationError("bad config")
        assert isinstance(err, BackboneError)
        assert err.category == "configuration"
        assert err.severity == "critical"
        assert err.retry_allowed is False

    def test_delivery_error(self):
        err = DeliveryError("tmux down")
        assert isinstance(err, BackboneError)
        assert err.category == "delivery"
        assert err.severity == "high"
        assert err.retry_allowed is True

    def test_state_error(self):
        err = StateError("corrupt state")
        assert isinstance(err, BackboneError)
        assert err.category == "state"
        assert err.severity == "medium"
        assert err.retry_allowed is False

    def test_external_service_error(self):
        err = ExternalServiceError("github 500")
        assert isinstance(err, BackboneError)
        assert err.category == "external"
        assert err.severity == "high"
        assert err.retry_allowed is True

    def test_catch_backbone_error_catches_subclasses(self):
        with pytest.raises(BackboneError):
            raise DeliveryError("fail")


# ── Lifecycle Manager ──────────────────────────────────────


class _TrackingComponent:
    """Component that records start/stop calls for testing."""

    def __init__(self, name: str, healthy: bool = True, fail_on: str | None = None):
        self._name = name
        self._healthy = healthy
        self._fail_on = fail_on
        self.started = False
        self.stopped = False
        self.calls: list[str] = []

    async def start(self) -> None:
        if self._fail_on == "start":
            raise RuntimeError(f"{self._name} failed to start")
        self.started = True
        self.calls.append("start")

    async def stop(self) -> None:
        if self._fail_on == "stop":
            raise RuntimeError(f"{self._name} failed to stop")
        self.stopped = True
        self.calls.append("stop")

    async def health_check(self) -> dict:
        if self._fail_on == "health":
            raise RuntimeError(f"{self._name} health check failed")
        return {"healthy": self._healthy, "name": self._name}


class TestLifecycleManager:
    def test_register_component(self):
        mgr = LifecycleManager()
        comp = _TrackingComponent("a")
        mgr.register("a", comp)

    def test_register_duplicate_raises(self):
        mgr = LifecycleManager()
        mgr.register("a", _TrackingComponent("a"))
        with pytest.raises(ValueError, match="already registered"):
            mgr.register("a", _TrackingComponent("a2"))

    async def test_start_all_in_order(self):
        mgr = LifecycleManager()
        order: list[str] = []

        class _Ordered:
            def __init__(self, name: str):
                self._name = name

            async def start(self):
                order.append(self._name)

            async def stop(self):
                pass

            async def health_check(self):
                return {"healthy": True}

        mgr.register("first", _Ordered("first"))
        mgr.register("second", _Ordered("second"))
        mgr.register("third", _Ordered("third"))

        await mgr.start_all()
        assert order == ["first", "second", "third"]

    async def test_stop_all_in_reverse_order(self):
        mgr = LifecycleManager()
        order: list[str] = []

        class _Ordered:
            def __init__(self, name: str):
                self._name = name

            async def start(self):
                pass

            async def stop(self):
                order.append(self._name)

            async def health_check(self):
                return {"healthy": True}

        mgr.register("first", _Ordered("first"))
        mgr.register("second", _Ordered("second"))
        mgr.register("third", _Ordered("third"))

        await mgr.start_all()
        await mgr.stop_all()
        assert order == ["third", "second", "first"]

    async def test_start_failure_rolls_back(self):
        mgr = LifecycleManager()
        a = _TrackingComponent("a")
        b = _TrackingComponent("b", fail_on="start")
        c = _TrackingComponent("c")

        mgr.register("a", a)
        mgr.register("b", b)
        mgr.register("c", c)

        with pytest.raises(RuntimeError, match="b failed to start"):
            await mgr.start_all()

        # a was started then rolled back, b failed, c never started
        assert a.started is True
        assert a.stopped is True
        assert b.started is False
        assert c.started is False

    async def test_stop_continues_on_error(self):
        mgr = LifecycleManager()
        a = _TrackingComponent("a")
        b = _TrackingComponent("b", fail_on="stop")
        c = _TrackingComponent("c")

        mgr.register("a", a)
        mgr.register("b", b)
        mgr.register("c", c)

        await mgr.start_all()
        # Should not raise — logs error but continues
        await mgr.stop_all()

        assert a.stopped is True
        assert c.stopped is True

    async def test_health_aggregation_all_healthy(self):
        mgr = LifecycleManager()
        mgr.register("a", _TrackingComponent("a", healthy=True))
        mgr.register("b", _TrackingComponent("b", healthy=True))

        result = await mgr.health()
        assert result["healthy"] is True
        assert "a" in result["components"]
        assert "b" in result["components"]

    async def test_health_aggregation_one_unhealthy(self):
        mgr = LifecycleManager()
        mgr.register("a", _TrackingComponent("a", healthy=True))
        mgr.register("b", _TrackingComponent("b", healthy=False))

        result = await mgr.health()
        assert result["healthy"] is False
        assert result["components"]["a"]["healthy"] is True
        assert result["components"]["b"]["healthy"] is False

    async def test_health_handles_exception(self):
        mgr = LifecycleManager()
        mgr.register("a", _TrackingComponent("a", fail_on="health"))

        result = await mgr.health()
        assert result["healthy"] is False
        assert result["components"]["a"]["healthy"] is False
        assert "error" in result["components"]["a"]
