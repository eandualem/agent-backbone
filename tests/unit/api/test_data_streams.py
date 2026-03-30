"""Tests for api/data_streams.py — real-time data stream namespaces."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_backbone.api.data_streams import (
    DataStreamNamespace,
    RunsNamespace,
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
    async def test_connect_bootstraps_initial_snapshot_to_sid(self):
        """New connections get an immediate targeted snapshot."""
        fetch = AsyncMock(return_value={"count": 1})
        ns = _make_namespace(fetch_fn=fetch)

        result = await ns.on_connect("sid1", {}, auth=None)
        await asyncio.sleep(0)

        assert result is True
        ns.server.emit.assert_awaited_once_with(
            "test:update",
            {"count": 1},
            namespace="/test",
            to="sid1",
        )

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
    def test_registers_10_snapshot_namespaces_and_runs_and_issues(self):
        """register_data_streams registers snapshot domains plus /runs and /issues namespaces."""
        import agent_backbone.api.data_streams as ds_mod
        from agent_backbone.api.data_streams import register_data_streams

        sio = MagicMock()

        with (
            patch("asyncio.create_task") as mock_create_task,
            patch("agent_backbone.services._locator.get_config") as mock_config,
        ):
            mock_create_task.return_value = MagicMock()
            mock_config.return_value.issue_domain.sync_interval_seconds = 300
            register_data_streams(sio)

        assert len(ds_mod._namespaces) == 10
        # 10 snapshot + 1 /issues + 1 /runs = 12
        assert sio.register_namespace.call_count == 12

    def test_registers_expected_domains(self):
        """All expected domain names are present in the registry."""
        import agent_backbone.api.data_streams as ds_mod
        from agent_backbone.api.data_streams import register_data_streams

        sio = MagicMock()

        with (
            patch("asyncio.create_task") as mock_create_task,
            patch("agent_backbone.services._locator.get_config") as mock_config,
        ):
            mock_create_task.return_value = MagicMock()
            mock_config.return_value.issue_domain.sync_interval_seconds = 300
            register_data_streams(sio)

        expected = {
            "agents",
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
        """Each snapshot namespace gets a background periodic emitter task, plus issue sync."""
        import agent_backbone.api.data_streams as ds_mod
        from agent_backbone.api.data_streams import register_data_streams

        sio = MagicMock()
        created_tasks = []

        def capture_task(*args, **kwargs):
            task = MagicMock()
            created_tasks.append(task)
            return task

        with (
            patch("asyncio.create_task", side_effect=capture_task),
            patch("agent_backbone.services._locator.get_config") as mock_config,
        ):
            mock_config.return_value.issue_domain.sync_interval_seconds = 300
            register_data_streams(sio)

        # 10 snapshot emitters + 1 issue-sync = 11
        assert len(created_tasks) == 11
        assert len(ds_mod._poll_tasks) == 11

    def test_registered_namespaces_are_data_stream_instances(self):
        """Every registered namespace is a DataStreamNamespace."""
        import agent_backbone.api.data_streams as ds_mod
        from agent_backbone.api.data_streams import register_data_streams

        sio = MagicMock()

        with (
            patch("asyncio.create_task") as mock_create_task,
            patch("agent_backbone.services._locator.get_config") as mock_config,
        ):
            mock_create_task.return_value = MagicMock()
            mock_config.return_value.issue_domain.sync_interval_seconds = 300
            register_data_streams(sio)

        for ns in ds_mod._namespaces.values():
            assert isinstance(ns, DataStreamNamespace)

    def test_registers_runs_namespace(self):
        """The discrete /runs namespace is registered alongside snapshot streams."""
        from agent_backbone.api.data_streams import register_data_streams

        sio = MagicMock()

        with (
            patch("asyncio.create_task") as mock_create_task,
            patch("agent_backbone.services._locator.get_config") as mock_config,
        ):
            mock_create_task.return_value = MagicMock()
            mock_config.return_value.issue_domain.sync_interval_seconds = 300
            register_data_streams(sio)

        namespaces = [call.args[0] for call in sio.register_namespace.call_args_list]
        assert any(isinstance(ns, RunsNamespace) and ns.namespace == "/runs" for ns in namespaces)

    @pytest.mark.asyncio
    async def test_agents_fetch_uses_live_issues_pending(self):
        """The /agents stream mirrors dashboard issue counts instead of hardcoding zero."""
        import agent_backbone.api.data_streams as ds_mod
        from agent_backbone.api.data_streams import register_data_streams

        sio = MagicMock()

        with (
            patch("asyncio.create_task") as mock_create_task,
            patch("agent_backbone.services._locator.get_config") as mock_config,
        ):
            mock_create_task.return_value = MagicMock()
            mock_config.return_value.issue_domain.sync_interval_seconds = 300
            register_data_streams(sio)

        agent = MagicMock()
        agent.model_dump.return_value = {"session": "ike"}
        counts = MagicMock(plan_waiting=2)
        counts.model_dump.return_value = {"total": 1}
        services = MagicMock()
        services.model_dump.return_value = {"gateway": "up", "database": "up"}

        with (
            patch(
                "agent_backbone.api.routes.dashboard._fetch_agents",
                AsyncMock(return_value=[agent]),
            ),
            patch("agent_backbone.api.routes.dashboard._compute_counts", return_value=counts),
            patch(
                "agent_backbone.api.routes.dashboard._fetch_failed_deliveries",
                AsyncMock(return_value=4),
            ),
            patch(
                "agent_backbone.api.routes.dashboard._fetch_issues_pending",
                AsyncMock(return_value=7),
            ),
            patch(
                "agent_backbone.api.routes.dashboard._fetch_service_health",
                AsyncMock(return_value=services),
            ),
            patch("agent_backbone.services._locator.get_config", return_value=MagicMock()),
            patch("agent_backbone.services._locator.get_db", return_value=MagicMock()),
            patch("agent_backbone.services._locator.get_gh", return_value=MagicMock()),
            patch("agent_backbone.services.agents.interface.StateService"),
            patch("agent_backbone.services.terminal.TmuxService"),
        ):
            payload = await ds_mod._namespaces["agents"].fetch_fn()

        assert payload["issues_pending"] == 7
        assert payload["failed_deliveries"] == 4
        assert payload["plans_pending"] == 2


class TestRunsNamespace:
    @pytest.mark.asyncio
    async def test_connect_allows_valid_key(self):
        """The /runs namespace uses the shared Socket.IO auth contract."""
        ns = RunsNamespace("/runs")
        with patch.dict(os.environ, {"BACKBONE_API_KEY": "secret"}):
            result = await ns.on_connect("sid1", {}, auth={"api_key": "secret"})
        assert result is True

    @pytest.mark.asyncio
    async def test_connect_rejects_invalid_key(self):
        """Invalid auth is rejected on /runs."""
        ns = RunsNamespace("/runs")
        with patch.dict(os.environ, {"BACKBONE_API_KEY": "secret"}):
            result = await ns.on_connect("sid1", {}, auth={"api_key": "wrong"})
        assert result is False

    @pytest.mark.asyncio
    async def test_subscribe_joins_run_rooms(self):
        """RDS-86B: subscribe joins run:{runId} rooms."""
        ns = RunsNamespace("/runs")
        ns.enter_room = AsyncMock()
        ns.emit = AsyncMock()
        ns.rooms = MagicMock(return_value=["sid1", "run:bug-validation-49"])

        await ns.on_subscribe("sid1", {"runs": ["bug-validation-49"]})

        ns.enter_room.assert_awaited_once_with("sid1", "run:bug-validation-49")
        ns.emit.assert_awaited_once_with(
            "subscribed",
            {"rooms": ["run:bug-validation-49"]},
            to="sid1",
        )


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
