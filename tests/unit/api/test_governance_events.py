"""Tests for api/run_events.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from agent_backbone.api.run_events import RUN_EVENT, emit_run_event


class TestEmitRunEvent:
    async def test_broadcasts_to_runs_namespace(self):
        """RDS-86A: Events without runId broadcast on /runs."""
        sio = AsyncMock()
        await emit_run_event(
            "action.executed",
            context={"runId": ""},
            source="track-engine",
            data={"action_type": "log"},
            sio=sio,
        )
        # Should emit once — broadcast only (no runId room)
        assert sio.emit.await_count == 1
        args, kwargs = sio.emit.await_args
        assert args[0] == RUN_EVENT
        payload = args[1]
        assert payload["type"] == "action.executed"
        assert payload["context"] == {"runId": ""}
        assert payload["source"] == "track-engine"
        assert payload["data"] == {"action_type": "log"}
        assert isinstance(payload["timestamp"], int)
        assert kwargs["namespace"] == "/runs"
        assert "room" not in kwargs

    async def test_routes_to_run_room_when_run_id_present(self):
        """RDS-86A: Events with runId emit to run room and broadcast."""
        sio = AsyncMock()
        await emit_run_event(
            "bug.claim.structured",
            context={"issue_id": 49, "runId": "bug-validation-49"},
            source="snow-wf",
            sio=sio,
        )
        assert sio.emit.await_count == 2
        calls = sio.emit.await_args_list
        run_call = next(call for call in calls if call.kwargs.get("room"))
        broadcast_call = next(call for call in calls if "room" not in call.kwargs)
        assert run_call.kwargs["room"] == "run:bug-validation-49"
        assert run_call.kwargs["namespace"] == "/runs"
        assert broadcast_call.kwargs["namespace"] == "/runs"

    async def test_defaults_context_and_data(self):
        """Context and data default to empty dicts."""
        sio = AsyncMock()
        await emit_run_event("test.event", sio=sio)
        payload = sio.emit.await_args[0][1]
        assert payload["context"] == {}
        assert payload["data"] == {}
        assert payload["source"] == "backbone"

    async def test_silent_when_sio_is_none(self):
        """No error when sio is None and locator returns None."""
        with patch("agent_backbone.api.run_events.get_sio", return_value=None):
            await emit_run_event("test.event")
        # Should not raise

    async def test_silent_on_emit_exception(self):
        """Swallows exceptions from sio.emit — never blocks caller."""
        sio = AsyncMock()
        sio.emit.side_effect = RuntimeError("socket down")
        await emit_run_event("test.event", sio=sio)
        # Should not raise

    async def test_uses_locator_when_no_sio_param(self):
        """Falls back to _locator.get_sio() when sio param is None."""
        mock_sio = AsyncMock()
        with patch("agent_backbone.api.run_events.get_sio", return_value=mock_sio):
            await emit_run_event("test.event")
        mock_sio.emit.assert_awaited_once()
