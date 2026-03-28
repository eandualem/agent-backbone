"""Tests for api/governance_events.py — governance event emission."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from agent_backbone.api.governance_events import GOVERNANCE_EVENT, emit_governance_event


class TestEmitGovernanceEvent:
    async def test_emits_to_all_agents_room(self):
        """SUB-10: Events without instanceId emit to all-agents room."""
        sio = AsyncMock()
        await emit_governance_event(
            "issue.created",
            context={"issue_id": 42, "repo": "eandualem/agent-backbone"},
            source="bell-wf",
            data={"action": "opened"},
            sio=sio,
        )
        # Should emit once — to all-agents only (no instanceId)
        assert sio.emit.await_count == 1
        args, kwargs = sio.emit.await_args
        assert args[0] == GOVERNANCE_EVENT
        payload = args[1]
        assert payload["type"] == "issue.created"
        assert payload["context"] == {"issue_id": 42, "repo": "eandualem/agent-backbone"}
        assert payload["source"] == "bell-wf"
        assert payload["data"] == {"action": "opened"}
        assert isinstance(payload["timestamp"], int)
        assert kwargs["namespace"] == "/sessions"
        assert kwargs["room"] == "all-agents"

    async def test_routes_to_track_room_when_instance_id_present(self):
        """SUB-10: Events with instanceId emit to track room AND all-agents."""
        sio = AsyncMock()
        await emit_governance_event(
            "bug.claim.structured",
            context={"issue_id": 49, "instanceId": "bug-validation-49"},
            source="snow-wf",
            sio=sio,
        )
        assert sio.emit.await_count == 2
        calls = sio.emit.await_args_list
        rooms = {call.kwargs["room"] for call in calls}
        assert rooms == {"track:bug-validation-49", "all-agents"}

    async def test_defaults_context_and_data(self):
        """Context and data default to empty dicts."""
        sio = AsyncMock()
        await emit_governance_event("test.event", sio=sio)
        payload = sio.emit.await_args[0][1]
        assert payload["context"] == {}
        assert payload["data"] == {}
        assert payload["source"] == "backbone"

    async def test_silent_when_sio_is_none(self):
        """No error when sio is None and locator returns None."""
        with patch("agent_backbone.api.governance_events.get_sio", return_value=None):
            await emit_governance_event("test.event")
        # Should not raise

    async def test_silent_on_emit_exception(self):
        """Swallows exceptions from sio.emit — never blocks caller."""
        sio = AsyncMock()
        sio.emit.side_effect = RuntimeError("socket down")
        await emit_governance_event("test.event", sio=sio)
        # Should not raise

    async def test_uses_locator_when_no_sio_param(self):
        """Falls back to _locator.get_sio() when sio param is None."""
        mock_sio = AsyncMock()
        with patch("agent_backbone.api.governance_events.get_sio", return_value=mock_sio):
            await emit_governance_event("test.event")
        mock_sio.emit.assert_awaited_once()
