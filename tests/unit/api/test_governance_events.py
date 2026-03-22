"""Tests for api/governance_events.py — governance event emission."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from agent_backbone.api.governance_events import GOVERNANCE_EVENT, emit_governance_event


class TestEmitGovernanceEvent:
    async def test_emits_correct_payload(self):
        """Emits governance:event with correct shape on /sessions namespace."""
        sio = AsyncMock()
        await emit_governance_event(
            "issue.created",
            context={"issue_id": 42, "repo": "eandualem/agent-backbone"},
            source="bell-wf",
            data={"action": "opened"},
            sio=sio,
        )
        sio.emit.assert_awaited_once()
        args, kwargs = sio.emit.await_args
        assert args[0] == GOVERNANCE_EVENT
        payload = args[1]
        assert payload["type"] == "issue.created"
        assert payload["context"] == {"issue_id": 42, "repo": "eandualem/agent-backbone"}
        assert payload["source"] == "bell-wf"
        assert payload["data"] == {"action": "opened"}
        assert isinstance(payload["timestamp"], int)
        assert kwargs["namespace"] == "/sessions"

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
