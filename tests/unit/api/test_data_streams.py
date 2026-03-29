"""Tests for api/data_streams.py — real-time data stream namespaces."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_backbone.api.data_streams import (
    DataStreamNamespace,
    _signature,
    get_data_namespace,
    notify_stream,
)


@pytest.fixture(autouse=True)
def _reset_namespaces():
    """Clear the module-level namespace registry between tests."""
    import agent_backbone.api.data_streams as ds_mod

    ds_mod._namespaces.clear()
    ds_mod._poll_tasks.clear()
    yield
    ds_mod._namespaces.clear()
    ds_mod._poll_tasks.clear()


def _make_namespace(
    ns_path: str = "/test",
    event: str = "test:update",
    fetch_fn: AsyncMock | None = None,
) -> DataStreamNamespace:
    """Create a DataStreamNamespace with a mocked server."""
    if fetch_fn is None:
        fetch_fn = AsyncMock(return_value={})
    ns = DataStreamNamespace(ns_path, event, fetch_fn)
    ns.server = MagicMock()
    ns.server.emit = AsyncMock()
    return ns


class TestDataStreamNamespace:
    @pytest.mark.asyncio
    async def test_connect_allows_dev_mode(self):
        """Connection allowed when BACKBONE_API_KEY is not set."""
        ns = _make_namespace()
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("BACKBONE_API_KEY", None)
            result = await ns.on_connect("sid1", {})
        assert result is True

    @pytest.mark.asyncio
    async def test_connect_rejects_invalid_key(self):
        """Connection rejected with wrong API key."""
        ns = _make_namespace()
        with patch.dict(os.environ, {"BACKBONE_API_KEY": "secret"}):
            result = await ns.on_connect("sid1", {}, auth={"api_key": "wrong"})
        assert result is False

    @pytest.mark.asyncio
    async def test_connect_allows_valid_key(self):
        """Connection allowed with correct API key."""
        ns = _make_namespace()
        with patch.dict(os.environ, {"BACKBONE_API_KEY": "secret"}):
            result = await ns.on_connect("sid1", {}, auth={"api_key": "secret"})
        assert result is True

    @pytest.mark.asyncio
    async def test_connect_rejects_missing_auth_when_key_set(self):
        """Connection rejected when API key is configured but no auth provided."""
        ns = _make_namespace()
        with patch.dict(os.environ, {"BACKBONE_API_KEY": "secret"}):
            result = await ns.on_connect("sid1", {})
        assert result is False

    @pytest.mark.asyncio
    async def test_connect_rejects_non_dict_auth(self):
        """Connection rejected when auth is not a dict."""
        ns = _make_namespace()
        with patch.dict(os.environ, {"BACKBONE_API_KEY": "secret"}):
            result = await ns.on_connect("sid1", {}, auth="not-a-dict")
        assert result is False

    @pytest.mark.asyncio
    async def test_emit_update_emits_on_first_call(self):
        """First emit always sends data (no previous signature)."""
        fetch = AsyncMock(return_value={"count": 1})
        ns = _make_namespace(fetch_fn=fetch)

        result = await ns.emit_update()

        assert result is True
        ns.server.emit.assert_awaited_once()
        args, kwargs = ns.server.emit.await_args
        assert args[0] == "test:update"
        assert args[1] == {"count": 1}
        assert kwargs["namespace"] == "/test"

    @pytest.mark.asyncio
    async def test_emit_update_suppresses_unchanged(self):
        """Unchanged data is not re-emitted (RDS-5)."""
        fetch = AsyncMock(return_value={"count": 1})
        ns = _make_namespace(fetch_fn=fetch)

        await ns.emit_update()
        result = await ns.emit_update()

        assert result is False
        assert ns.server.emit.await_count == 1

    @pytest.mark.asyncio
    async def test_emit_update_force_bypasses_dedup(self):
        """Force=True emits even when data hasn't changed."""
        fetch = AsyncMock(return_value={"count": 1})
        ns = _make_namespace(fetch_fn=fetch)

        await ns.emit_update()
        result = await ns.emit_update(force=True)

        assert result is True
        assert ns.server.emit.await_count == 2

    @pytest.mark.asyncio
    async def test_emit_update_emits_on_change(self):
        """Changed data triggers emission."""
        call_count = 0

        async def changing_fetch():
            nonlocal call_count
            call_count += 1
            return {"count": call_count}

        ns = _make_namespace(fetch_fn=changing_fetch)

        await ns.emit_update()
        result = await ns.emit_update()

        assert result is True
        assert ns.server.emit.await_count == 2

    @pytest.mark.asyncio
    async def test_emit_update_handles_fetch_error(self):
        """Fetch errors are caught and logged, not propagated."""
        fetch = AsyncMock(side_effect=RuntimeError("db down"))
        ns = _make_namespace(fetch_fn=fetch)

        result = await ns.emit_update()

        assert result is False
        ns.server.emit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reset_clears_dedup_state(self):
        """Reset allows re-emission of same data."""
        fetch = AsyncMock(return_value={"count": 1})
        ns = _make_namespace(fetch_fn=fetch)

        await ns.emit_update()
        ns.reset()
        result = await ns.emit_update()

        assert result is True
        assert ns.server.emit.await_count == 2

    @pytest.mark.asyncio
    async def test_emit_update_stores_last_signature(self):
        """After emit, the internal signature state is updated."""
        fetch = AsyncMock(return_value={"x": 42})
        ns = _make_namespace(fetch_fn=fetch)

        assert ns._last_signature is None
        await ns.emit_update()
        assert ns._last_signature is not None
        assert ns._last_signature == _signature({"x": 42})

    @pytest.mark.asyncio
    async def test_reset_sets_signature_to_none(self):
        """Reset sets internal signature back to None."""
        fetch = AsyncMock(return_value={"x": 1})
        ns = _make_namespace(fetch_fn=fetch)

        await ns.emit_update()
        assert ns._last_signature is not None
        ns.reset()
        assert ns._last_signature is None


class TestSignature:
    def test_stable_key_ordering(self):
        """Signature is stable regardless of dict key insertion order."""
        a = _signature({"b": 2, "a": 1})
        b = _signature({"a": 1, "b": 2})
        assert a == b

    def test_different_data_different_signature(self):
        """Different data produces different signatures."""
        a = _signature({"count": 1})
        b = _signature({"count": 2})
        assert a != b

    def test_nested_dict_stability(self):
        """Nested dicts are also sorted for stable signatures."""
        a = _signature({"outer": {"b": 2, "a": 1}})
        b = _signature({"outer": {"a": 1, "b": 2}})
        assert a == b

    def test_list_order_matters(self):
        """List element order affects signature (lists are ordered)."""
        a = _signature({"items": [1, 2, 3]})
        b = _signature({"items": [3, 2, 1]})
        assert a != b

    def test_returns_string(self):
        """Signature returns a string (JSON)."""
        result = _signature({"key": "value"})
        assert isinstance(result, str)


class TestGetDataNamespace:
    def test_returns_none_for_unknown_domain(self):
        """Unknown domain returns None."""
        result = get_data_namespace("nonexistent")
        assert result is None

    def test_returns_registered_namespace(self):
        """Registered namespace is returned by domain name."""
        import agent_backbone.api.data_streams as ds_mod

        ns = _make_namespace("/agents", "agents:update")
        ds_mod._namespaces["agents"] = ns

        result = get_data_namespace("agents")
        assert result is ns


class TestNotifyStream:
    @pytest.mark.asyncio
    async def test_triggers_emit_on_registered_domain(self):
        """notify_stream calls emit_update on the matching namespace."""
        import agent_backbone.api.data_streams as ds_mod

        fetch = AsyncMock(return_value={"data": 1})
        ns = _make_namespace("/tasks", "tasks:update", fetch)
        ds_mod._namespaces["tasks"] = ns

        await notify_stream("tasks")

        ns.server.emit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_noops_for_unknown_domain(self):
        """notify_stream silently does nothing for unregistered domains."""
        # Should not raise
        await notify_stream("nonexistent")

    @pytest.mark.asyncio
    async def test_default_force_is_true(self):
        """notify_stream defaults to force=True, bypassing change detection."""
        import agent_backbone.api.data_streams as ds_mod

        fetch = AsyncMock(return_value={"data": 1})
        ns = _make_namespace("/rooms", "rooms:update", fetch)
        ds_mod._namespaces["rooms"] = ns

        # First call populates signature
        await ns.emit_update()
        ns.server.emit.reset_mock()

        # notify_stream should still emit (force=True by default)
        await notify_stream("rooms")
        ns.server.emit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_force_false_respects_change_detection(self):
        """notify_stream with force=False skips if data unchanged."""
        import agent_backbone.api.data_streams as ds_mod

        fetch = AsyncMock(return_value={"data": 1})
        ns = _make_namespace("/rooms", "rooms:update", fetch)
        ds_mod._namespaces["rooms"] = ns

        # First call populates signature
        await ns.emit_update()
        ns.server.emit.reset_mock()

        # force=False should suppress since data is unchanged
        await notify_stream("rooms", force=False)
        ns.server.emit.assert_not_awaited()


class TestRegisterDataStreams:
    def test_registers_all_11_namespaces(self):
        """register_data_streams registers exactly 11 namespace domains."""
        import agent_backbone.api.data_streams as ds_mod
        from agent_backbone.api.data_streams import register_data_streams

        sio = MagicMock()

        with patch("asyncio.create_task") as mock_create_task:
            mock_create_task.return_value = MagicMock()
            register_data_streams(sio)

        assert len(ds_mod._namespaces) == 11
        assert sio.register_namespace.call_count == 11

    def test_registers_expected_domains(self):
        """All expected domain names are present in the registry."""
        import agent_backbone.api.data_streams as ds_mod
        from agent_backbone.api.data_streams import register_data_streams

        sio = MagicMock()

        with patch("asyncio.create_task") as mock_create_task:
            mock_create_task.return_value = MagicMock()
            register_data_streams(sio)

        expected = {
            "agents",
            "tasks",
            "rooms",
            "activity",
            "flows",
            "swarms",
            "plans",
            "repos",
            "notes",
            "schedule",
            "settings",
        }
        assert set(ds_mod._namespaces.keys()) == expected

    def test_creates_periodic_emitter_tasks(self):
        """Each namespace gets a background periodic emitter task."""
        import agent_backbone.api.data_streams as ds_mod
        from agent_backbone.api.data_streams import register_data_streams

        sio = MagicMock()
        created_tasks = []

        def capture_task(*args, **kwargs):
            task = MagicMock()
            created_tasks.append(task)
            return task

        with patch("asyncio.create_task", side_effect=capture_task):
            register_data_streams(sio)

        assert len(created_tasks) == 11
        assert len(ds_mod._poll_tasks) == 11

    def test_registered_namespaces_are_data_stream_instances(self):
        """Every registered namespace is a DataStreamNamespace."""
        import agent_backbone.api.data_streams as ds_mod
        from agent_backbone.api.data_streams import register_data_streams

        sio = MagicMock()

        with patch("asyncio.create_task") as mock_create_task:
            mock_create_task.return_value = MagicMock()
            register_data_streams(sio)

        for ns in ds_mod._namespaces.values():
            assert isinstance(ns, DataStreamNamespace)


class TestStopDataStreams:
    @pytest.mark.asyncio
    async def test_cancels_and_clears_tasks(self):
        """stop_data_streams cancels all poll tasks and clears registries."""
        import agent_backbone.api.data_streams as ds_mod
        from agent_backbone.api.data_streams import stop_data_streams

        # Create a real task that sleeps forever (simulates a poll loop)
        async def _forever():
            await asyncio.sleep(3600)

        task = asyncio.create_task(_forever())
        ds_mod._poll_tasks.append(task)
        ds_mod._namespaces["test"] = _make_namespace()

        await stop_data_streams()

        assert task.cancelled()
        assert len(ds_mod._poll_tasks) == 0
        assert len(ds_mod._namespaces) == 0
