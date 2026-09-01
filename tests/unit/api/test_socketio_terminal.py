"""The Socket.IO ``/terminal`` namespace streams registered agents only."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent_backbone.api.socketio_server import TerminalNamespace

_MOD = "agent_backbone.api.socketio_server"


def _namespace(config) -> TerminalNamespace:
    ns = TerminalNamespace("/terminal")
    ns.server = SimpleNamespace(fastapi_app=SimpleNamespace(state=SimpleNamespace(config=config)))
    ns.emit = AsyncMock()
    ns._attach_subscription_client = AsyncMock()
    ns.enter_room = AsyncMock()
    return ns


class TestTerminalJoin:
    async def test_unregistered_tmux_session_is_refused(self, config):
        ns = _namespace(config)
        with patch(f"{_MOD}.session_exists", new_callable=AsyncMock, return_value=True) as ex:
            await ns.on_join("sid1", {"session": "stray", "cols": 80, "rows": 24})
        ns.emit.assert_awaited_once()
        assert "not a registered agent" in ns.emit.await_args.args[1]["message"]
        ns._attach_subscription_client.assert_not_awaited()
        ex.assert_not_awaited()  # refused before touching tmux

    async def test_non_string_session_is_rejected_cleanly(self, config):
        # A JSON array/object as "session" must not reach the registry lookup
        # (unhashable → TypeError); it is a malformed request, nothing more.
        ns = _namespace(config)
        for bad in (["ike"], {"name": "ike"}, 7):
            await ns.on_join("sid1", {"session": bad})
        assert ns.emit.await_count == 3
        assert all("Missing session name" in c.args[1]["message"] for c in ns.emit.await_args_list)
        ns._attach_subscription_client.assert_not_called()

    async def test_registered_agent_is_streamed(self, config):
        ns = _namespace(config)
        with patch(f"{_MOD}.session_exists", new_callable=AsyncMock, return_value=True):
            await ns.on_join("sid1", {"session": "ike", "cols": 80, "rows": 24})
        ns._attach_subscription_client.assert_awaited_once_with("sid1", "ike", 80, 24)
        ns.emit.assert_not_awaited()
