"""Tests for agent_backbone/jarvis.py — HTTP injection delivery and session discovery."""

from __future__ import annotations

import json

import httpx
import respx

from agent_backbone.jarvis import discover_session, inject_message

INJECT_URL = "https://console.example.com/api/assistant/inject"
SESSIONS_URL = "https://console.example.com/api/sessions"
EXPLICIT_SESSIONS_URL = "https://console.example.com/custom/sessions"


class TestDiscoverSession:
    @respx.mock
    async def test_success(self):
        """Returns id from first session object."""
        respx.get(SESSIONS_URL).respond(200, json={"sessions": [{"id": "abc-123"}]})
        result = await discover_session(INJECT_URL)
        assert result == "abc-123"

    @respx.mock
    async def test_bare_list_fallback(self):
        """Handles bare list response (no wrapping dict)."""
        respx.get(SESSIONS_URL).respond(200, json=[{"id": "bare-id"}])
        result = await discover_session(INJECT_URL)
        assert result == "bare-id"

    @respx.mock
    async def test_empty_list(self):
        """Returns None when no sessions exist."""
        respx.get(SESSIONS_URL).respond(200, json={"sessions": []})
        result = await discover_session(INJECT_URL)
        assert result is None

    @respx.mock
    async def test_server_error(self):
        """Returns None on non-200 response."""
        respx.get(SESSIONS_URL).respond(500, text="Internal Server Error")
        result = await discover_session(INJECT_URL)
        assert result is None

    @respx.mock
    async def test_network_error(self):
        """Returns None on connection failure."""
        respx.get(SESSIONS_URL).mock(side_effect=httpx.ConnectError("refused"))
        result = await discover_session(INJECT_URL)
        assert result is None

    @respx.mock
    async def test_url_derivation(self):
        """Derives /api/sessions from inject URL by stripping to /api/ base."""
        route = respx.get(SESSIONS_URL).respond(200, json={"sessions": [{"id": "s1"}]})
        await discover_session(INJECT_URL)
        assert route.calls[0].request.url == SESSIONS_URL

    @respx.mock
    async def test_explicit_sessions_url(self):
        """Uses explicit sessions_url when provided, ignoring derivation."""
        route = respx.get(EXPLICIT_SESSIONS_URL).respond(
            200, json={"sessions": [{"id": "explicit-s"}]}
        )
        result = await discover_session(INJECT_URL, sessions_url=EXPLICIT_SESSIONS_URL)
        assert result == "explicit-s"
        assert route.calls[0].request.url == EXPLICIT_SESSIONS_URL


class TestInjectMessage:
    @respx.mock
    async def test_successful_injection_with_explicit_session(self):
        """200 response returns True when session_id provided."""
        respx.post(INJECT_URL).respond(200, json={"ok": True})
        result = await inject_message(INJECT_URL, "Hello Jarvis", session_id="sess-1")
        assert result is True

    @respx.mock
    async def test_payload_format(self):
        """Verify JSON payload contains from, via, message, and sessionId."""
        route = respx.post(INJECT_URL).respond(200)
        await inject_message(INJECT_URL, "Test message", session_id="sess-1")

        request = route.calls[0].request
        body = json.loads(request.content)
        assert body == {
            "from": "backbone",
            "via": "backbone",
            "message": "Test message",
            "sessionId": "sess-1",
        }

    @respx.mock
    async def test_server_error_returns_false(self):
        """500 response returns False."""
        respx.post(INJECT_URL).respond(500, text="Internal Server Error")
        result = await inject_message(INJECT_URL, "Hello", session_id="sess-1")
        assert result is False

    @respx.mock
    async def test_network_error_returns_false(self):
        """Network error returns False."""
        respx.post(INJECT_URL).mock(side_effect=httpx.ConnectError("Connection refused"))
        result = await inject_message(INJECT_URL, "Hello", session_id="sess-1")
        assert result is False

    @respx.mock
    async def test_201_accepted(self):
        """201 response returns True (any 2xx is success)."""
        respx.post(INJECT_URL).respond(201)
        result = await inject_message(INJECT_URL, "Hello", session_id="sess-1")
        assert result is True

    @respx.mock
    async def test_auto_discovery(self):
        """When session_id is None, discovers session then injects."""
        respx.get(SESSIONS_URL).respond(200, json={"sessions": [{"id": "auto-sess"}]})
        route = respx.post(INJECT_URL).respond(200)

        result = await inject_message(INJECT_URL, "Auto message")
        assert result is True

        body = json.loads(route.calls[0].request.content)
        assert body["sessionId"] == "auto-sess"

    @respx.mock
    async def test_defers_when_no_session(self):
        """Returns False when discovery finds no session."""
        respx.get(SESSIONS_URL).respond(200, json={"sessions": []})
        result = await inject_message(INJECT_URL, "Hello")
        assert result is False

    @respx.mock
    async def test_explicit_session_skips_discovery(self):
        """When session_id is provided, no GET /sessions call is made."""
        respx.post(INJECT_URL).respond(200)
        await inject_message(INJECT_URL, "Hello", session_id="explicit")
        # If a GET to /sessions was made, respx would raise since it's not mocked
