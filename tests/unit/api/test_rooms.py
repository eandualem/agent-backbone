"""Tests for api/routes/rooms.py — room message delivery via deliver_message."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.api.models import Room


def _create_room(tmp_path, *, state="active", participants=None, moderator="bell"):
    """Create a room JSON file and return its id."""
    room_dir = tmp_path / ".claude" / "state" / "rooms"
    room_dir.mkdir(parents=True, exist_ok=True)
    room_id = str(uuid.uuid4())
    room = Room(
        id=room_id,
        title="Test Room",
        description="A test room",
        moderator=moderator,
        participants=participants or ["ike", "feynman"],
        state=state,
        transcript=[],
        created_at=1700000000.0,
        updated_at=1700000000.0,
    )
    (room_dir / f"{room_id}.json").write_text(room.model_dump_json(indent=2))
    return room_id


@pytest.fixture(autouse=True)
def _patch_room_dir(tmp_path):
    """Point room storage to tmp_path for all tests."""
    room_dir = tmp_path / ".claude" / "state" / "rooms"
    room_dir.mkdir(parents=True, exist_ok=True)
    with patch("agent_backbone.api.routes.rooms._ROOM_DIR", room_dir):
        yield


@pytest.fixture(autouse=True)
def _patch_resolve():
    """Patch resolve_entity_session to identity-map entity -> session name."""

    def _identity(entity, config):
        return entity

    with patch(
        "agent_backbone.api.routes.rooms._resolve_entity_session",
        side_effect=_identity,
    ):
        yield


# ---------------------------------------------------------------------------
# send_directed — deliver_message integration
# ---------------------------------------------------------------------------


class TestSendDirected:
    async def test_uses_deliver_message(self, api_client, auth_headers, tmp_path, api_app):
        """Directed message calls deliver_message with correct args."""
        room_id = _create_room(tmp_path)
        mock_deliver = AsyncMock(return_value="delivered")
        with patch("agent_backbone.api.routes.rooms.deliver_message", mock_deliver):
            resp = await api_client.post(
                f"/api/rooms/{room_id}/directed",
                json={"target": "ike", "content": "Hello Ike"},
                headers=auth_headers,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["status"] == "delivered"
        assert data["target"] == "ike"
        mock_deliver.assert_awaited_once()
        call_args = mock_deliver.call_args
        assert call_args[0][0] == "ike"
        assert "Hello Ike" in call_args[0][1]

    async def test_non_delivered_status(self, api_client, auth_headers, tmp_path, api_app):
        """When deliver_message returns a non-delivered status, ok=False."""
        room_id = _create_room(tmp_path)
        mock_deliver = AsyncMock(return_value="offline")
        with patch("agent_backbone.api.routes.rooms.deliver_message", mock_deliver):
            resp = await api_client.post(
                f"/api/rooms/{room_id}/directed",
                json={"target": "ike", "content": "Hello"},
                headers=auth_headers,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert data["status"] == "offline"

    async def test_closed_room_returns_400(self, api_client, auth_headers, tmp_path, api_app):
        """Sending to a closed room is rejected with 400."""
        room_id = _create_room(tmp_path, state="closed")
        mock_deliver = AsyncMock(return_value="delivered")
        with patch("agent_backbone.api.routes.rooms.deliver_message", mock_deliver):
            resp = await api_client.post(
                f"/api/rooms/{room_id}/directed",
                json={"target": "ike", "content": "Hello"},
                headers=auth_headers,
            )

        assert resp.status_code == 400
        assert "closed" in resp.json()["detail"].lower()

    async def test_paused_room_returns_400(self, api_client, auth_headers, tmp_path, api_app):
        """Sending to a paused room is rejected with 400."""
        room_id = _create_room(tmp_path, state="paused")
        mock_deliver = AsyncMock(return_value="delivered")
        with patch("agent_backbone.api.routes.rooms.deliver_message", mock_deliver):
            resp = await api_client.post(
                f"/api/rooms/{room_id}/directed",
                json={"target": "ike", "content": "Hello"},
                headers=auth_headers,
            )

        assert resp.status_code == 400
        assert "paused" in resp.json()["detail"].lower()

    async def test_jarvis_target(self, api_client, auth_headers, tmp_path, api_app):
        """Jarvis target passes through to deliver_message."""
        room_id = _create_room(tmp_path, participants=["jarvis", "feynman"])
        mock_deliver = AsyncMock(return_value="delivered")
        with patch("agent_backbone.api.routes.rooms.deliver_message", mock_deliver):
            resp = await api_client.post(
                f"/api/rooms/{room_id}/directed",
                json={"target": "jarvis", "content": "Hello Jarvis"},
                headers=auth_headers,
            )

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert mock_deliver.call_args[0][0] == "jarvis"


# ---------------------------------------------------------------------------
# send_broadcast — deliver_message integration
# ---------------------------------------------------------------------------


class TestSendBroadcast:
    async def test_uses_deliver_message_for_all_participants(
        self, api_client, auth_headers, tmp_path, api_app
    ):
        """Broadcast calls deliver_message for each participant."""
        room_id = _create_room(tmp_path, participants=["ike", "feynman", "ada"])
        mock_deliver = AsyncMock(return_value="delivered")
        with patch("agent_backbone.api.routes.rooms.deliver_message", mock_deliver):
            resp = await api_client.post(
                f"/api/rooms/{room_id}/broadcast",
                json={"content": "Hello everyone"},
                headers=auth_headers,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["delivered"] == 3
        assert data["failed"] == 0
        assert data["total"] == 3
        assert mock_deliver.await_count == 3

    async def test_partial_delivery(self, api_client, auth_headers, tmp_path, api_app):
        """When some participants are offline, counts reflect partial delivery."""
        room_id = _create_room(tmp_path, participants=["ike", "feynman"])

        async def _side_effect(session, msg, config, **kwargs):
            return "delivered" if session == "ike" else "offline"

        mock_deliver = AsyncMock(side_effect=_side_effect)
        with patch("agent_backbone.api.routes.rooms.deliver_message", mock_deliver):
            resp = await api_client.post(
                f"/api/rooms/{room_id}/broadcast",
                json={"content": "Hello"},
                headers=auth_headers,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert data["delivered"] == 1
        assert data["failed"] == 1

    async def test_closed_room_returns_400(self, api_client, auth_headers, tmp_path, api_app):
        """Broadcasting to a closed room is rejected with 400."""
        room_id = _create_room(tmp_path, state="closed")
        mock_deliver = AsyncMock(return_value="delivered")
        with patch("agent_backbone.api.routes.rooms.deliver_message", mock_deliver):
            resp = await api_client.post(
                f"/api/rooms/{room_id}/broadcast",
                json={"content": "Hello"},
                headers=auth_headers,
            )

        assert resp.status_code == 400
        assert "closed" in resp.json()["detail"].lower()

    async def test_paused_room_returns_400(self, api_client, auth_headers, tmp_path, api_app):
        """Broadcasting to a paused room is rejected with 400."""
        room_id = _create_room(tmp_path, state="paused")
        mock_deliver = AsyncMock(return_value="delivered")
        with patch("agent_backbone.api.routes.rooms.deliver_message", mock_deliver):
            resp = await api_client.post(
                f"/api/rooms/{room_id}/broadcast",
                json={"content": "Hello"},
                headers=auth_headers,
            )

        assert resp.status_code == 400
        assert "paused" in resp.json()["detail"].lower()
