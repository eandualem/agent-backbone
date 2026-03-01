"""Tests for api/routes/rooms.py — room message delivery via session_bridge."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.deps import get_delivery_service
from api.models import Room


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


def _make_mock_delivery_svc(safe_deliver_return="delivered", safe_deliver_side_effect=None):
    """Create a mock DeliveryService with safe_deliver configured."""
    svc = MagicMock()
    svc.safe_deliver = AsyncMock(
        return_value=safe_deliver_return,
        side_effect=safe_deliver_side_effect,
    )
    return svc


@pytest.fixture(autouse=True)
def _patch_room_dir(tmp_path):
    """Point room storage to tmp_path for all tests."""
    room_dir = tmp_path / ".claude" / "state" / "rooms"
    room_dir.mkdir(parents=True, exist_ok=True)
    with patch("api.routes.rooms._ROOM_DIR", room_dir):
        yield


# ---------------------------------------------------------------------------
# send_directed — session_bridge integration
# ---------------------------------------------------------------------------


class TestSendDirected:
    async def test_uses_safe_deliver(self, api_client, auth_headers, tmp_path, api_app):
        """Directed message calls safe_deliver with correct args."""
        room_id = _create_room(tmp_path)
        mock_svc = _make_mock_delivery_svc()
        api_app.dependency_overrides[get_delivery_service] = lambda: mock_svc
        try:
            resp = await api_client.post(
                f"/api/rooms/{room_id}/directed",
                json={"target": "ike", "content": "Hello Ike"},
                headers=auth_headers,
            )
        finally:
            api_app.dependency_overrides.pop(get_delivery_service, None)

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["status"] == "delivered"
        assert data["target"] == "ike"
        mock_svc.safe_deliver.assert_awaited_once()
        call_args = mock_svc.safe_deliver.call_args
        assert call_args[0][0] == "ike"
        assert "Hello Ike" in call_args[0][1]

    async def test_non_delivered_status(self, api_client, auth_headers, tmp_path, api_app):
        """When safe_deliver returns a non-delivered status, ok=False."""
        room_id = _create_room(tmp_path)
        mock_svc = _make_mock_delivery_svc(safe_deliver_return="agent_working")
        api_app.dependency_overrides[get_delivery_service] = lambda: mock_svc
        try:
            resp = await api_client.post(
                f"/api/rooms/{room_id}/directed",
                json={"target": "ike", "content": "Hello"},
                headers=auth_headers,
            )
        finally:
            api_app.dependency_overrides.pop(get_delivery_service, None)

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert data["status"] == "agent_working"

    async def test_closed_room_returns_400(self, api_client, auth_headers, tmp_path, api_app):
        """Sending to a closed room is rejected with 400."""
        room_id = _create_room(tmp_path, state="closed")
        mock_svc = _make_mock_delivery_svc()
        api_app.dependency_overrides[get_delivery_service] = lambda: mock_svc
        try:
            resp = await api_client.post(
                f"/api/rooms/{room_id}/directed",
                json={"target": "ike", "content": "Hello"},
                headers=auth_headers,
            )
        finally:
            api_app.dependency_overrides.pop(get_delivery_service, None)

        assert resp.status_code == 400
        assert "closed" in resp.json()["detail"].lower()

    async def test_paused_room_returns_400(self, api_client, auth_headers, tmp_path, api_app):
        """Sending to a paused room is rejected with 400."""
        room_id = _create_room(tmp_path, state="paused")
        mock_svc = _make_mock_delivery_svc()
        api_app.dependency_overrides[get_delivery_service] = lambda: mock_svc
        try:
            resp = await api_client.post(
                f"/api/rooms/{room_id}/directed",
                json={"target": "ike", "content": "Hello"},
                headers=auth_headers,
            )
        finally:
            api_app.dependency_overrides.pop(get_delivery_service, None)

        assert resp.status_code == 400
        assert "paused" in resp.json()["detail"].lower()

    async def test_jarvis_target(self, api_client, auth_headers, tmp_path, api_app):
        """Jarvis target passes through to safe_deliver (bridge handles HTTP path)."""
        room_id = _create_room(tmp_path, participants=["jarvis", "feynman"])
        mock_svc = _make_mock_delivery_svc()
        api_app.dependency_overrides[get_delivery_service] = lambda: mock_svc
        try:
            resp = await api_client.post(
                f"/api/rooms/{room_id}/directed",
                json={"target": "jarvis", "content": "Hello Jarvis"},
                headers=auth_headers,
            )
        finally:
            api_app.dependency_overrides.pop(get_delivery_service, None)

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert mock_svc.safe_deliver.call_args[0][0] == "jarvis"


# ---------------------------------------------------------------------------
# send_broadcast — session_bridge integration
# ---------------------------------------------------------------------------


class TestSendBroadcast:
    async def test_uses_safe_deliver_for_all_participants(
        self, api_client, auth_headers, tmp_path, api_app
    ):
        """Broadcast calls safe_deliver for each participant."""
        room_id = _create_room(tmp_path, participants=["ike", "feynman", "ada"])
        mock_svc = _make_mock_delivery_svc()
        api_app.dependency_overrides[get_delivery_service] = lambda: mock_svc
        try:
            resp = await api_client.post(
                f"/api/rooms/{room_id}/broadcast",
                json={"content": "Hello everyone"},
                headers=auth_headers,
            )
        finally:
            api_app.dependency_overrides.pop(get_delivery_service, None)

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["delivered"] == 3
        assert data["failed"] == 0
        assert data["total"] == 3
        assert mock_svc.safe_deliver.await_count == 3

    async def test_partial_delivery(self, api_client, auth_headers, tmp_path, api_app):
        """When some participants are offline, counts reflect partial delivery."""
        room_id = _create_room(tmp_path, participants=["ike", "feynman"])

        async def _side_effect(session, msg, config):
            return "delivered" if session == "ike" else "offline"

        mock_svc = _make_mock_delivery_svc(safe_deliver_side_effect=_side_effect)
        api_app.dependency_overrides[get_delivery_service] = lambda: mock_svc
        try:
            resp = await api_client.post(
                f"/api/rooms/{room_id}/broadcast",
                json={"content": "Hello"},
                headers=auth_headers,
            )
        finally:
            api_app.dependency_overrides.pop(get_delivery_service, None)

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert data["delivered"] == 1
        assert data["failed"] == 1

    async def test_closed_room_returns_400(self, api_client, auth_headers, tmp_path, api_app):
        """Broadcasting to a closed room is rejected with 400."""
        room_id = _create_room(tmp_path, state="closed")
        mock_svc = _make_mock_delivery_svc()
        api_app.dependency_overrides[get_delivery_service] = lambda: mock_svc
        try:
            resp = await api_client.post(
                f"/api/rooms/{room_id}/broadcast",
                json={"content": "Hello"},
                headers=auth_headers,
            )
        finally:
            api_app.dependency_overrides.pop(get_delivery_service, None)

        assert resp.status_code == 400
        assert "closed" in resp.json()["detail"].lower()

    async def test_paused_room_returns_400(self, api_client, auth_headers, tmp_path, api_app):
        """Broadcasting to a paused room is rejected with 400."""
        room_id = _create_room(tmp_path, state="paused")
        mock_svc = _make_mock_delivery_svc()
        api_app.dependency_overrides[get_delivery_service] = lambda: mock_svc
        try:
            resp = await api_client.post(
                f"/api/rooms/{room_id}/broadcast",
                json={"content": "Hello"},
                headers=auth_headers,
            )
        finally:
            api_app.dependency_overrides.pop(get_delivery_service, None)

        assert resp.status_code == 400
        assert "paused" in resp.json()["detail"].lower()
