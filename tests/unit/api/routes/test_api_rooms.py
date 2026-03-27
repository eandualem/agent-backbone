"""Tests for api/routes/rooms.py — room management endpoints."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_backbone.api.deps import get_config
from agent_backbone.api.models import Room, RoomMessage
from agent_backbone.api.routes.rooms import (
    _compute_context_delta,
    _format_meeting_setup_envelope,
    _format_room_message,
    _inject_meeting_skill,
    _read_meeting_skill,
)

_DELIVER_PATCH = "agent_backbone.api.routes.rooms.deliver_message"


@pytest.fixture
def room_dir(tmp_path):
    """Patch _ROOM_DIR to use tmp_path for isolation."""
    d = tmp_path / "rooms"
    d.mkdir()
    with patch("agent_backbone.api.routes.rooms._ROOM_DIR", d):
        yield d


def _save_room_file(room_dir, room: Room):
    """Helper to write a room JSON file directly."""
    path = room_dir / f"{room.id}.json"
    path.write_text(room.model_dump_json(indent=2))


def _make_room(**kwargs) -> Room:
    """Create a Room with defaults."""
    defaults = {
        "id": "test-room-1",
        "title": "Architecture review",
        "moderator": "ike",
        "participants": ["leo", "feynman"],
        "state": "active",
        "transcript": [],
        "created_at": 1000.0,
        "updated_at": 1000.0,
    }
    defaults.update(kwargs)
    return Room(**defaults)


def _make_message(**kwargs) -> RoomMessage:
    """Create a RoomMessage with defaults."""
    defaults = {
        "id": "msg-1",
        "sender": "ike",
        "recipients": ["leo"],
        "mode": "directed",
        "content": "What do you think?",
        "timestamp": 1001.0,
    }
    defaults.update(kwargs)
    return RoomMessage(**defaults)


class TestCreateRoom:
    """Tests for POST /api/rooms."""

    async def test_create_room_success(self, api_client, auth_headers, room_dir, api_app):
        """201 with UUID id, state=active, empty transcript, file persisted, injection triggered."""
        mock_deliver = AsyncMock(return_value="delivered")
        mock_config = MagicMock()
        api_app.dependency_overrides[get_config] = lambda: mock_config
        with patch(_DELIVER_PATCH, mock_deliver):
            with patch(
                "agent_backbone.api.routes.rooms._inject_meeting_skill",
                new_callable=AsyncMock,
            ) as mock_inject:
                resp = await api_client.post(
                    "/api/rooms",
                    headers=auth_headers,
                    json={
                        "title": "Sprint planning",
                        "description": "Q1 sprint scope and assignments",
                        "moderator": "ike",
                        "participants": ["leo", "feynman"],
                    },
                )

                assert resp.status_code == 201
                data = resp.json()
                assert len(data["id"]) == 36  # UUID4 length
                assert data["title"] == "Sprint planning"
                assert data["description"] == "Q1 sprint scope and assignments"
                assert data["moderator"] == "ike"
                assert data["participants"] == ["leo", "feynman"]
                assert data["state"] == "active"
                assert data["transcript"] == []
                assert isinstance(data["created_at"], str) and "T" in data["created_at"]
                assert isinstance(data["updated_at"], str) and "T" in data["updated_at"]

                # Verify JSON file persisted
                room_file = room_dir / f"{data['id']}.json"
                assert room_file.exists()
                saved = json.loads(room_file.read_text())
                assert saved["title"] == "Sprint planning"
                assert saved["description"] == "Q1 sprint scope and assignments"

                # Verify meeting skill injection was triggered
                mock_inject.assert_called_once()
                injected_room = mock_inject.call_args[0][0]
                assert injected_room.id == data["id"]
        # deliver_message patched via context manager
            api_app.dependency_overrides.pop(get_config, None)


class TestListRooms:
    """Tests for GET /api/rooms."""

    async def test_list_rooms_empty(self, api_client, auth_headers, room_dir):
        """200 with total=0 when no rooms exist."""
        resp = await api_client.get("/api/rooms", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    async def test_list_rooms_returns_all(self, api_client, auth_headers, room_dir):
        """200 with all rooms returned."""
        room1 = _make_room(id="room-1", title="Topic A")
        room2 = _make_room(id="room-2", title="Topic B")
        _save_room_file(room_dir, room1)
        _save_room_file(room_dir, room2)

        resp = await api_client.get("/api/rooms", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        titles = {item["title"] for item in data["items"]}
        assert titles == {"Topic A", "Topic B"}

    async def test_list_rooms_filtered_by_state(self, api_client, auth_headers, room_dir):
        """Only rooms matching state filter are returned."""
        room_active = _make_room(id="room-a", state="active")
        room_closed = _make_room(id="room-c", state="closed")
        _save_room_file(room_dir, room_active)
        _save_room_file(room_dir, room_closed)

        resp = await api_client.get("/api/rooms?state=closed", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["id"] == "room-c"


class TestGetRoom:
    """Tests for GET /api/rooms/{room_id}."""

    async def test_get_room_success(self, api_client, auth_headers, room_dir):
        """200 with full transcript included."""
        msg = _make_message()
        room = _make_room(transcript=[msg])
        _save_room_file(room_dir, room)

        resp = await api_client.get(f"/api/rooms/{room.id}", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == room.id
        assert len(data["transcript"]) == 1
        assert data["transcript"][0]["content"] == "What do you think?"

    async def test_get_room_not_found(self, api_client, auth_headers, room_dir):
        """404 for non-existent room."""
        resp = await api_client.get("/api/rooms/no-such-room", headers=auth_headers)
        assert resp.status_code == 404


class TestSendDirected:
    """Tests for POST /api/rooms/{room_id}/directed."""

    async def test_send_directed_success(self, api_client, auth_headers, room_dir, api_app):
        """200, safe_deliver called, message appended with mode=directed."""
        mock_deliver = AsyncMock(return_value="delivered")
        with patch(_DELIVER_PATCH, mock_deliver):
            room = _make_room()
            _save_room_file(room_dir, room)

            with patch(
                "agent_backbone.api.routes.rooms._resolve_entity_session",
                return_value="leo",
            ):
                resp = await api_client.post(
                    f"/api/rooms/{room.id}/directed",
                    headers=auth_headers,
                    json={"target": "leo", "content": "What's your take?"},
                )

            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            assert data["status"] == "delivered"
            assert data["target"] == "leo"

            # Verify deliver_message was called
            mock_deliver.assert_called_once()
            call_args = mock_deliver.call_args
            assert call_args[0][0] == "leo"
            assert "What's your take?" in call_args[0][1]

            # Verify message in transcript
            saved = Room.model_validate_json((room_dir / f"{room.id}.json").read_text())
            assert len(saved.transcript) == 1
            assert saved.transcript[0].mode == "directed"
            assert saved.transcript[0].sender == "ike"
        # deliver_message patched via context manager

    async def test_send_directed_target_not_participant(
        self, api_client, auth_headers, room_dir, api_app
    ):
        """400 when target is not a room participant."""
        mock_deliver = AsyncMock(return_value="delivered")
        with patch(_DELIVER_PATCH, mock_deliver):
            room = _make_room(participants=["leo", "feynman"])
            _save_room_file(room_dir, room)

            resp = await api_client.post(
                f"/api/rooms/{room.id}/directed",
                headers=auth_headers,
                json={"target": "ada", "content": "Hello"},
            )

            assert resp.status_code == 400
            assert "not a room participant" in resp.json()["detail"]
            mock_deliver.assert_not_called()
        # deliver_message patched via context manager

    async def test_send_directed_room_not_found(self, api_client, auth_headers, room_dir, api_app):
        """404 for non-existent room."""
        mock_deliver = AsyncMock(return_value="delivered")
        with patch(_DELIVER_PATCH, mock_deliver):
            resp = await api_client.post(
                "/api/rooms/no-room/directed",
                headers=auth_headers,
                json={"target": "leo", "content": "Hello"},
            )
            assert resp.status_code == 404
        # deliver_message patched via context manager


class TestPeerDirected:
    """Tests for peer-to-peer directed messaging with moderator CC."""

    async def test_peer_directed_delivery(self, api_client, auth_headers, room_dir, api_app):
        """Participant sends to another participant — both get delivery, moderator gets CC."""
        mock_deliver = AsyncMock(return_value="delivered")
        with patch(_DELIVER_PATCH, mock_deliver):
            room = _make_room(participants=["leo", "feynman"])
            _save_room_file(room_dir, room)

            with patch(
                "agent_backbone.api.routes.rooms._resolve_entity_session",
                side_effect=["feynman-session", "ike-session"],
            ):
                resp = await api_client.post(
                    f"/api/rooms/{room.id}/directed",
                    headers=auth_headers,
                    json={"target": "feynman", "content": "Your thoughts?", "sender": "leo"},
                )

            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            assert data["status"] == "delivered"
            assert data["target"] == "feynman"
            assert data["sender"] == "leo"

            # target + moderator CC = 2 deliveries
            assert mock_deliver.call_count == 2
        # deliver_message patched via context manager

    async def test_peer_directed_moderator_cc(self, api_client, auth_headers, room_dir, api_app):
        """Non-moderator sender triggers CC to moderator with moderator's own delta."""
        mock_deliver = AsyncMock(return_value="delivered")
        with patch(_DELIVER_PATCH, mock_deliver):
            room = _make_room(participants=["leo", "feynman"])
            _save_room_file(room_dir, room)

            sessions_resolved = []

            def _track_resolve(entity, config):
                sessions_resolved.append(entity)
                return f"{entity}-session"

            with patch(
                "agent_backbone.api.routes.rooms._resolve_entity_session",
                side_effect=_track_resolve,
            ):
                resp = await api_client.post(
                    f"/api/rooms/{room.id}/directed",
                    headers=auth_headers,
                    json={"target": "feynman", "content": "Hello", "sender": "leo"},
                )

            assert resp.status_code == 200
            # Both target and moderator were resolved
            assert "feynman" in sessions_resolved
            assert "ike" in sessions_resolved

            # Transcript records only the target, not the CC
            saved = Room.model_validate_json((room_dir / f"{room.id}.json").read_text())
            assert len(saved.transcript) == 1
            assert saved.transcript[0].sender == "leo"
            assert saved.transcript[0].recipients == ["feynman"]
        # deliver_message patched via context manager

    async def test_peer_directed_to_moderator_no_cc(
        self, api_client, auth_headers, room_dir, api_app
    ):
        """Participant messages moderator — no CC (moderator IS the target)."""
        mock_deliver = AsyncMock(return_value="delivered")
        with patch(_DELIVER_PATCH, mock_deliver):
            room = _make_room(participants=["leo", "feynman"])
            _save_room_file(room_dir, room)

            with patch(
                "agent_backbone.api.routes.rooms._resolve_entity_session",
                return_value="ike-session",
            ):
                resp = await api_client.post(
                    f"/api/rooms/{room.id}/directed",
                    headers=auth_headers,
                    json={"target": "ike", "content": "Question for you", "sender": "leo"},
                )

            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            assert data["sender"] == "leo"
            assert data["target"] == "ike"

            # Only 1 delivery — no CC when moderator is the target
            assert mock_deliver.call_count == 1
        # deliver_message patched via context manager

    async def test_moderator_directed_no_cc(self, api_client, auth_headers, room_dir, api_app):
        """Moderator sends — no CC (sender IS the moderator)."""
        mock_deliver = AsyncMock(return_value="delivered")
        with patch(_DELIVER_PATCH, mock_deliver):
            room = _make_room(participants=["leo", "feynman"])
            _save_room_file(room_dir, room)

            with patch(
                "agent_backbone.api.routes.rooms._resolve_entity_session",
                return_value="leo-session",
            ):
                resp = await api_client.post(
                    f"/api/rooms/{room.id}/directed",
                    headers=auth_headers,
                    json={"target": "leo", "content": "What do you think?"},
                )

            assert resp.status_code == 200
            # Only 1 delivery — moderator is the default sender, no CC
            assert mock_deliver.call_count == 1
        # deliver_message patched via context manager

    async def test_directed_sender_not_participant(
        self, api_client, auth_headers, room_dir, api_app
    ):
        """400 for unknown sender."""
        mock_deliver = AsyncMock(return_value="delivered")
        with patch(_DELIVER_PATCH, mock_deliver):
            room = _make_room(participants=["leo", "feynman"])
            _save_room_file(room_dir, room)

            resp = await api_client.post(
                f"/api/rooms/{room.id}/directed",
                headers=auth_headers,
                json={"target": "leo", "content": "Hello", "sender": "ada"},
            )

            assert resp.status_code == 400
            assert "Sender is not a room participant or moderator" in resp.json()["detail"]
            mock_deliver.assert_not_called()
        # deliver_message patched via context manager

    async def test_directed_sender_equals_target(self, api_client, auth_headers, room_dir, api_app):
        """400 for self-message."""
        mock_deliver = AsyncMock(return_value="delivered")
        with patch(_DELIVER_PATCH, mock_deliver):
            room = _make_room(participants=["leo", "feynman"])
            _save_room_file(room_dir, room)

            resp = await api_client.post(
                f"/api/rooms/{room.id}/directed",
                headers=auth_headers,
                json={"target": "leo", "content": "Hello", "sender": "leo"},
            )

            assert resp.status_code == 400
            assert "Sender and target must be different" in resp.json()["detail"]
            mock_deliver.assert_not_called()
        # deliver_message patched via context manager

    async def test_directed_sender_cursor_advances(
        self, api_client, auth_headers, room_dir, api_app
    ):
        """Sender's cursor advances on send — they know what they sent."""
        mock_deliver = AsyncMock(return_value="delivered")
        with patch(_DELIVER_PATCH, mock_deliver):
            room = _make_room(participants=["leo", "feynman"])
            _save_room_file(room_dir, room)

            with patch(
                "agent_backbone.api.routes.rooms._resolve_entity_session",
                side_effect=["feynman-session", "ike-session"],
            ):
                resp = await api_client.post(
                    f"/api/rooms/{room.id}/directed",
                    headers=auth_headers,
                    json={"target": "feynman", "content": "Check this", "sender": "leo"},
                )

            assert resp.status_code == 200
            saved = Room.model_validate_json((room_dir / f"{room.id}.json").read_text())
            # Sender (leo) cursor should be advanced
            assert saved.cursors.get("leo") == 1
            # Target (feynman) cursor should also advance on successful delivery
            assert saved.cursors.get("feynman") == 1
            # Moderator (ike) cursor should also advance (CC delivered)
            assert saved.cursors.get("ike") == 1
        # deliver_message patched via context manager

    async def test_peer_directed_cc_failure_doesnt_fail_request(
        self, api_client, auth_headers, room_dir, api_app
    ):
        """CC fails but ok=true if target delivery succeeded."""
        # First call (target) succeeds, second call (CC to moderator) fails
        mock_deliver = AsyncMock(side_effect=["delivered", "offline"])
        with patch(_DELIVER_PATCH, mock_deliver):
            room = _make_room(participants=["leo", "feynman"])
            _save_room_file(room_dir, room)

            with patch(
                "agent_backbone.api.routes.rooms._resolve_entity_session",
                side_effect=["feynman-session", "ike-session"],
            ):
                resp = await api_client.post(
                    f"/api/rooms/{room.id}/directed",
                    headers=auth_headers,
                    json={"target": "feynman", "content": "Hey", "sender": "leo"},
                )

            assert resp.status_code == 200
            data = resp.json()
            # Primary delivery to target succeeded
            assert data["ok"] is True
            assert data["status"] == "delivered"

            # Moderator cursor should NOT advance (CC failed)
            saved = Room.model_validate_json((room_dir / f"{room.id}.json").read_text())
            assert saved.cursors.get("ike") is None or saved.cursors.get("ike") == 0
        # deliver_message patched via context manager

    async def test_directed_target_can_be_moderator(
        self, api_client, auth_headers, room_dir, api_app
    ):
        """Participant can message the moderator directly."""
        mock_deliver = AsyncMock(return_value="delivered")
        with patch(_DELIVER_PATCH, mock_deliver):
            room = _make_room(participants=["leo", "feynman"])
            _save_room_file(room_dir, room)

            with patch(
                "agent_backbone.api.routes.rooms._resolve_entity_session",
                return_value="ike-session",
            ):
                resp = await api_client.post(
                    f"/api/rooms/{room.id}/directed",
                    headers=auth_headers,
                    json={"target": "ike", "content": "Moderator question", "sender": "feynman"},
                )

            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            assert data["target"] == "ike"
            assert data["sender"] == "feynman"

            saved = Room.model_validate_json((room_dir / f"{room.id}.json").read_text())
            assert saved.transcript[0].sender == "feynman"
            assert saved.transcript[0].recipients == ["ike"]
        # deliver_message patched via context manager


class TestSendBroadcast:
    """Tests for POST /api/rooms/{room_id}/broadcast."""

    async def test_send_broadcast_success(self, api_client, auth_headers, room_dir, api_app):
        """200, all delivered, message appended with mode=broadcast."""
        mock_deliver = AsyncMock(return_value="delivered")
        with patch(_DELIVER_PATCH, mock_deliver):
            room = _make_room(participants=["leo", "feynman"])
            _save_room_file(room_dir, room)

            with patch(
                "agent_backbone.api.routes.rooms._resolve_entity_session",
                side_effect=["leo-session", "feynman-session"],
            ):
                resp = await api_client.post(
                    f"/api/rooms/{room.id}/broadcast",
                    headers=auth_headers,
                    json={"content": "Let's discuss the architecture"},
                )

            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            assert data["delivered"] == 2
            assert data["failed"] == 0
            assert data["total"] == 2

            # Verify deliver_message called for each participant
            assert mock_deliver.call_count == 2

            # Verify transcript
            saved = Room.model_validate_json((room_dir / f"{room.id}.json").read_text())
            assert len(saved.transcript) == 1
            assert saved.transcript[0].mode == "broadcast"
            assert set(saved.transcript[0].recipients) == {"leo", "feynman"}
        # deliver_message patched via context manager

    async def test_send_broadcast_partial_failure(
        self, api_client, auth_headers, room_dir, api_app
    ):
        """Partial delivery: delivered=1, failed=1, message still appended."""
        mock_deliver = AsyncMock(side_effect=["delivered", "offline"])
        with patch(_DELIVER_PATCH, mock_deliver):
            room = _make_room(participants=["leo", "feynman"])
            _save_room_file(room_dir, room)

            with patch(
                "agent_backbone.api.routes.rooms._resolve_entity_session",
                side_effect=["leo-session", "feynman-session"],
            ):
                resp = await api_client.post(
                    f"/api/rooms/{room.id}/broadcast",
                    headers=auth_headers,
                    json={"content": "Broadcast test"},
                )

            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is False
            assert data["delivered"] == 1
            assert data["failed"] == 1
            assert data["total"] == 2

            # Message still in transcript
            saved = Room.model_validate_json((room_dir / f"{room.id}.json").read_text())
            assert len(saved.transcript) == 1
        # deliver_message patched via context manager

    async def test_send_broadcast_room_not_found(self, api_client, auth_headers, room_dir, api_app):
        """404 for non-existent room."""
        mock_deliver = AsyncMock(return_value="delivered")
        with patch(_DELIVER_PATCH, mock_deliver):
            resp = await api_client.post(
                "/api/rooms/no-room/broadcast",
                headers=auth_headers,
                json={"content": "Hello all"},
            )
            assert resp.status_code == 404
        # deliver_message patched via context manager


class TestPostResponse:
    """Tests for POST /api/rooms/{room_id}/respond."""

    async def test_post_response_success(self, api_client, auth_headers, room_dir, api_app):
        """200, message appended with mode=response, delivered to moderator."""
        mock_deliver = AsyncMock(return_value="delivered")
        with patch(_DELIVER_PATCH, mock_deliver):
            room = _make_room()
            _save_room_file(room_dir, room)

            with patch(
                "agent_backbone.api.routes.rooms._resolve_entity_session",
                return_value="ike-session",
            ):
                resp = await api_client.post(
                    f"/api/rooms/{room.id}/respond",
                    headers=auth_headers,
                    json={"sender": "leo", "content": "I think we should go with option A"},
                )

            assert resp.status_code == 200
            assert resp.json()["ok"] is True

            # Verify transcript
            saved = Room.model_validate_json((room_dir / f"{room.id}.json").read_text())
            assert len(saved.transcript) == 1
            assert saved.transcript[0].mode == "response"
            assert saved.transcript[0].sender == "leo"
            assert saved.transcript[0].content == "I think we should go with option A"

            # Moderator received delivery
            mock_deliver.assert_called_once()
            assert mock_deliver.call_args[0][0] == "ike-session"
        # deliver_message patched via context manager

    async def test_post_response_room_not_found(self, api_client, auth_headers, room_dir, api_app):
        """404 for non-existent room."""
        mock_deliver = AsyncMock(return_value="delivered")
        with patch(_DELIVER_PATCH, mock_deliver):
            resp = await api_client.post(
                "/api/rooms/no-room/respond",
                headers=auth_headers,
                json={"sender": "leo", "content": "Hello"},
            )
            assert resp.status_code == 404
        # deliver_message patched via context manager

    @pytest.mark.parametrize("state", ["paused", "closed"])
    async def test_post_response_rejects_inactive_room(
        self, api_client, auth_headers, room_dir, api_app, state
    ):
        """400 when posting to a paused or closed room."""
        mock_deliver = AsyncMock(return_value="delivered")
        with patch(_DELIVER_PATCH, mock_deliver):
            room = _make_room(state=state)
            _save_room_file(room_dir, room)

            resp = await api_client.post(
                f"/api/rooms/{room.id}/respond",
                headers=auth_headers,
                json={"sender": "leo", "content": "Still there?"},
            )

            assert resp.status_code == 400
            saved = Room.model_validate_json((room_dir / f"{room.id}.json").read_text())
            assert saved.transcript == []
            mock_deliver.assert_not_called()
        # deliver_message patched via context manager

    async def test_post_response_sender_not_participant(
        self, api_client, auth_headers, room_dir, api_app
    ):
        """400 when sender is not a room participant or moderator."""
        mock_deliver = AsyncMock(return_value="delivered")
        with patch(_DELIVER_PATCH, mock_deliver):
            room = _make_room(participants=["leo", "feynman"])
            _save_room_file(room_dir, room)

            resp = await api_client.post(
                f"/api/rooms/{room.id}/respond",
                headers=auth_headers,
                json={"sender": "ada", "content": "Let me in"},
            )

            assert resp.status_code == 400
            assert "Sender is not a room participant or moderator" in resp.json()["detail"]
            saved = Room.model_validate_json((room_dir / f"{room.id}.json").read_text())
            assert saved.transcript == []
            mock_deliver.assert_not_called()
        # deliver_message patched via context manager

    async def test_post_response_moderator_no_self_delivery(
        self, api_client, auth_headers, room_dir, api_app
    ):
        """Moderator's own response doesn't trigger self-delivery."""
        mock_deliver = AsyncMock(return_value="delivered")
        with patch(_DELIVER_PATCH, mock_deliver):
            room = _make_room()
            _save_room_file(room_dir, room)

            resp = await api_client.post(
                f"/api/rooms/{room.id}/respond",
                headers=auth_headers,
                json={"sender": "ike", "content": "Moderator note"},
            )

            assert resp.status_code == 200
            assert resp.json()["ok"] is True

            # No delivery — moderator is the sender
            mock_deliver.assert_not_called()
        # deliver_message patched via context manager

    async def test_post_response_sender_cursor_advances(
        self, api_client, auth_headers, room_dir, api_app
    ):
        """Sender's cursor advances unconditionally on response."""
        mock_deliver = AsyncMock(return_value="delivered")
        with patch(_DELIVER_PATCH, mock_deliver):
            room = _make_room()
            _save_room_file(room_dir, room)

            with patch(
                "agent_backbone.api.routes.rooms._resolve_entity_session",
                return_value="ike-session",
            ):
                resp = await api_client.post(
                    f"/api/rooms/{room.id}/respond",
                    headers=auth_headers,
                    json={"sender": "leo", "content": "My response"},
                )

            assert resp.status_code == 200
            saved = Room.model_validate_json((room_dir / f"{room.id}.json").read_text())
            assert saved.cursors.get("leo") == 1
        # deliver_message patched via context manager

    async def test_post_response_moderator_cursor_advances_on_delivery(
        self, api_client, auth_headers, room_dir, api_app
    ):
        """Moderator's cursor advances on successful delivery."""
        mock_deliver = AsyncMock(return_value="delivered")
        with patch(_DELIVER_PATCH, mock_deliver):
            room = _make_room()
            _save_room_file(room_dir, room)

            with patch(
                "agent_backbone.api.routes.rooms._resolve_entity_session",
                return_value="ike-session",
            ):
                resp = await api_client.post(
                    f"/api/rooms/{room.id}/respond",
                    headers=auth_headers,
                    json={"sender": "feynman", "content": "Agreed"},
                )

            assert resp.status_code == 200
            saved = Room.model_validate_json((room_dir / f"{room.id}.json").read_text())
            assert saved.cursors.get("ike") == 1
            assert saved.cursors.get("feynman") == 1
        # deliver_message patched via context manager

    async def test_post_response_delivery_failure_doesnt_fail_request(
        self, api_client, auth_headers, room_dir, api_app
    ):
        """Delivery failure doesn't affect API response — ok still True."""
        mock_deliver = AsyncMock(return_value="offline")
        with patch(_DELIVER_PATCH, mock_deliver):
            room = _make_room()
            _save_room_file(room_dir, room)

            with patch(
                "agent_backbone.api.routes.rooms._resolve_entity_session",
                return_value="ike-session",
            ):
                resp = await api_client.post(
                    f"/api/rooms/{room.id}/respond",
                    headers=auth_headers,
                    json={"sender": "leo", "content": "Testing"},
                )

            assert resp.status_code == 200
            assert resp.json()["ok"] is True

            # Moderator cursor should NOT advance (delivery failed)
            saved = Room.model_validate_json((room_dir / f"{room.id}.json").read_text())
            assert saved.cursors.get("ike") is None or saved.cursors.get("ike") == 0
            # But sender cursor still advances
            assert saved.cursors.get("leo") == 1
        # deliver_message patched via context manager

    async def test_post_response_unresolvable_moderator(
        self, api_client, auth_headers, room_dir, api_app
    ):
        """Unresolvable moderator session doesn't fail the request."""
        mock_deliver = AsyncMock(return_value="delivered")
        with patch(_DELIVER_PATCH, mock_deliver):
            room = _make_room()
            _save_room_file(room_dir, room)

            with patch(
                "agent_backbone.api.routes.rooms._resolve_entity_session",
                return_value=None,
            ):
                resp = await api_client.post(
                    f"/api/rooms/{room.id}/respond",
                    headers=auth_headers,
                    json={"sender": "leo", "content": "Can you hear me?"},
                )

            assert resp.status_code == 200
            assert resp.json()["ok"] is True
            mock_deliver.assert_not_called()
        # deliver_message patched via context manager


class TestUpdateRoomState:
    """Tests for PATCH /api/rooms/{room_id}."""

    @pytest.fixture(autouse=True)
    def _override_deps(self, api_app):
        """Provide config override and patch deliver_message for all state-update tests."""
        mock_config = MagicMock()
        api_app.dependency_overrides[get_config] = lambda: mock_config
        with patch(_DELIVER_PATCH, AsyncMock(return_value="delivered")):
            yield
        api_app.dependency_overrides.pop(get_config, None)

    async def test_update_room_state_success(self, api_client, auth_headers, room_dir):
        """200, state changed."""
        room = _make_room(state="active")
        _save_room_file(room_dir, room)

        resp = await api_client.patch(
            f"/api/rooms/{room.id}",
            headers=auth_headers,
            json={"state": "closed"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "closed"

        # Verify persisted
        saved = Room.model_validate_json((room_dir / f"{room.id}.json").read_text())
        assert saved.state == "closed"

    async def test_update_room_state_invalid(self, api_client, auth_headers, room_dir):
        """422 for invalid state value."""
        room = _make_room()
        _save_room_file(room_dir, room)

        resp = await api_client.patch(
            f"/api/rooms/{room.id}",
            headers=auth_headers,
            json={"state": "invalid_state"},
        )

        assert resp.status_code == 422
        assert "Invalid state" in resp.json()["detail"]

    async def test_update_room_state_not_found(self, api_client, auth_headers, room_dir):
        """404 for non-existent room."""
        resp = await api_client.patch(
            "/api/rooms/no-room",
            headers=auth_headers,
            json={"state": "closed"},
        )
        assert resp.status_code == 404

    async def test_paused_to_active_triggers_injection(self, api_client, auth_headers, room_dir):
        """Transitioning from paused to active triggers meeting skill injection."""
        room = _make_room(state="paused")
        _save_room_file(room_dir, room)

        with patch(
            "agent_backbone.api.routes.rooms._inject_meeting_skill",
            new_callable=AsyncMock,
        ) as mock_inject:
            resp = await api_client.patch(
                f"/api/rooms/{room.id}",
                headers=auth_headers,
                json={"state": "active"},
            )

        assert resp.status_code == 200
        assert resp.json()["state"] == "active"
        mock_inject.assert_called_once()
        injected_room = mock_inject.call_args[0][0]
        assert injected_room.id == room.id

    async def test_closed_to_active_triggers_injection(self, api_client, auth_headers, room_dir):
        """Transitioning from closed to active triggers meeting skill injection."""
        room = _make_room(state="closed")
        _save_room_file(room_dir, room)

        with patch(
            "agent_backbone.api.routes.rooms._inject_meeting_skill",
            new_callable=AsyncMock,
        ) as mock_inject:
            resp = await api_client.patch(
                f"/api/rooms/{room.id}",
                headers=auth_headers,
                json={"state": "active"},
            )

        assert resp.status_code == 200
        mock_inject.assert_called_once()

    async def test_active_to_active_does_not_trigger_injection(
        self, api_client, auth_headers, room_dir
    ):
        """Setting active on already-active room does NOT trigger injection."""
        room = _make_room(state="active")
        _save_room_file(room_dir, room)

        with patch(
            "agent_backbone.api.routes.rooms._inject_meeting_skill",
            new_callable=AsyncMock,
        ) as mock_inject:
            resp = await api_client.patch(
                f"/api/rooms/{room.id}",
                headers=auth_headers,
                json={"state": "active"},
            )

        assert resp.status_code == 200
        mock_inject.assert_not_called()

    async def test_active_to_paused_does_not_trigger_injection(
        self, api_client, auth_headers, room_dir
    ):
        """Transitioning from active to paused does NOT trigger injection."""
        room = _make_room(state="active")
        _save_room_file(room_dir, room)

        with patch(
            "agent_backbone.api.routes.rooms._inject_meeting_skill",
            new_callable=AsyncMock,
        ) as mock_inject:
            resp = await api_client.patch(
                f"/api/rooms/{room.id}",
                headers=auth_headers,
                json={"state": "paused"},
            )

        assert resp.status_code == 200
        mock_inject.assert_not_called()


class TestContextDelta:
    """Tests for _compute_context_delta cursor-based logic."""

    def test_no_cursor_returns_all(self):
        """Returns all messages when participant has no cursor (default 0)."""
        msg1 = _make_message(id="m1", sender="ike", content="Question 1")
        msg2 = _make_message(id="m2", sender="ike", content="Question 2")
        room = _make_room(transcript=[msg1, msg2])

        delta = _compute_context_delta(room, "leo")

        assert len(delta) == 2
        assert delta[0].id == "m1"
        assert delta[1].id == "m2"

    def test_cursor_returns_subsequent(self):
        """Returns only messages after the participant's cursor position."""
        msg1 = _make_message(id="m1", sender="ike", content="Q1")
        msg2 = _make_message(id="m2", sender="ike", content="Q2")
        msg3 = _make_message(id="m3", sender="ike", content="Q3")
        room = _make_room(transcript=[msg1, msg2, msg3], cursors={"leo": 2})

        delta = _compute_context_delta(room, "leo")

        assert len(delta) == 1
        assert delta[0].id == "m3"

    def test_cursor_at_end_returns_empty(self):
        """Returns empty list when cursor is at end of transcript."""
        msg1 = _make_message(id="m1", sender="ike", content="Q1")
        room = _make_room(transcript=[msg1], cursors={"leo": 1})

        delta = _compute_context_delta(room, "leo")

        assert delta == []

    def test_empty_transcript(self):
        """Returns empty list for empty transcript."""
        room = _make_room(transcript=[])

        delta = _compute_context_delta(room, "leo")

        assert delta == []


class TestFormatRoomMessage:
    """Tests for _format_room_message."""

    def test_with_context(self):
        """Includes context section when delta is non-empty."""
        msg = _make_message(id="m1", sender="ike", content="Earlier question")
        room = _make_room(description="Discuss architecture tradeoffs")
        delta = [msg]

        result = _format_room_message(room, "ike", "New question", "leo", delta)

        assert "[via:room room:test-room-1 from:ike]" in result
        assert "Title: Architecture review" in result
        assert "Description: Discuss architecture tradeoffs" in result
        assert "--- Context (since your last participation) ---" in result
        assert "[ike]: Earlier question" in result
        assert "[meeting: Architecture review] [from: ike]" in result
        assert "New question" in result

    def test_no_context(self):
        """No context section when delta is empty."""
        room = _make_room()

        result = _format_room_message(room, "ike", "First question", "leo", [])

        assert "[via:room room:test-room-1 from:ike]" in result
        assert "Title: Architecture review" in result
        assert "Context" not in result
        assert "[meeting: Architecture review] [from: ike]" in result
        assert "First question" in result

    def test_no_description(self):
        """Description line omitted when empty."""
        room = _make_room(description="")

        result = _format_room_message(room, "ike", "Question", "leo", [])

        assert "Title: Architecture review" in result
        assert "Description:" not in result


class TestBroadcastSessionResolution:
    """Tests for entity->session resolution in broadcast delivery."""

    async def test_send_broadcast_resolves_sessions(
        self, api_client, auth_headers, room_dir, api_app
    ):
        """resolve_entity_session called per participant; resolved name passed to safe_deliver."""
        mock_deliver = AsyncMock(return_value="delivered")
        with patch(_DELIVER_PATCH, mock_deliver):
            room = _make_room(participants=["leo", "feynman"])
            _save_room_file(room_dir, room)

            with patch(
                "agent_backbone.api.routes.rooms._resolve_entity_session",
                side_effect=["leo-session", "feynman-session"],
            ) as mock_resolve:
                resp = await api_client.post(
                    f"/api/rooms/{room.id}/broadcast",
                    headers=auth_headers,
                    json={"content": "Hello everyone"},
                )

            assert resp.status_code == 200
            data = resp.json()
            assert data["delivered"] == 2
            assert data["failed"] == 0

            # Verify resolution called for each participant
            assert mock_resolve.call_count == 2
            resolve_targets = [c.args[0] for c in mock_resolve.call_args_list]
            assert "leo" in resolve_targets
            assert "feynman" in resolve_targets

            # Verify safe_deliver received resolved session names
            deliver_sessions = [c.args[0] for c in mock_deliver.call_args_list]
            assert "leo-session" in deliver_sessions
            assert "feynman-session" in deliver_sessions
        # deliver_message patched via context manager

    async def test_send_broadcast_unresolvable_participant(
        self, api_client, auth_headers, room_dir, api_app
    ):
        """Unresolvable participant counted as failed, not delivered."""
        mock_deliver = AsyncMock(return_value="delivered")
        with patch(_DELIVER_PATCH, mock_deliver):
            room = _make_room(participants=["leo", "unknown-entity"])
            _save_room_file(room_dir, room)

            with patch(
                "agent_backbone.api.routes.rooms._resolve_entity_session",
                side_effect=["leo-session", None],
            ):
                resp = await api_client.post(
                    f"/api/rooms/{room.id}/broadcast",
                    headers=auth_headers,
                    json={"content": "Hello everyone"},
                )

            assert resp.status_code == 200
            data = resp.json()
            assert data["delivered"] == 1
            assert data["failed"] == 1
            assert data["total"] == 2

            # safe_deliver called only for the resolvable participant
            assert mock_deliver.call_count == 1
        # deliver_message patched via context manager

    async def test_send_broadcast_exception_logged(
        self, api_client, auth_headers, room_dir, api_app
    ):
        """Exceptions from gather are caught; counted as failed."""
        mock_deliver = AsyncMock(return_value="delivered")
        with patch(_DELIVER_PATCH, mock_deliver):
            room = _make_room(participants=["leo", "feynman"])
            _save_room_file(room_dir, room)

            # First participant resolves fine, second raises
            def _resolve_side_effect(target, config):
                if target == "leo":
                    return "leo-session"
                raise RuntimeError("tmux error")

            with patch(
                "agent_backbone.api.routes.rooms._resolve_entity_session",
                side_effect=_resolve_side_effect,
            ):
                resp = await api_client.post(
                    f"/api/rooms/{room.id}/broadcast",
                    headers=auth_headers,
                    json={"content": "Test"},
                )

            assert resp.status_code == 200
            data = resp.json()
            assert data["delivered"] == 1
            assert data["failed"] == 1
            # Message still appended to transcript
            saved = Room.model_validate_json((room_dir / f"{room.id}.json").read_text())
            assert len(saved.transcript) == 1
        # deliver_message patched via context manager


class TestDirectedSessionResolution:
    """Tests for entity->session resolution in directed delivery."""

    async def test_send_directed_resolves_session(
        self, api_client, auth_headers, room_dir, api_app
    ):
        """resolve_entity_session called; resolved name passed to safe_deliver."""
        mock_deliver = AsyncMock(return_value="delivered")
        with patch(_DELIVER_PATCH, mock_deliver):
            room = _make_room()
            _save_room_file(room_dir, room)

            with patch(
                "agent_backbone.api.routes.rooms._resolve_entity_session",
                return_value="leo-session",
            ) as mock_resolve:
                resp = await api_client.post(
                    f"/api/rooms/{room.id}/directed",
                    headers=auth_headers,
                    json={"target": "leo", "content": "What's your take?"},
                )

            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            assert data["status"] == "delivered"

            # Verify resolution called
            mock_resolve.assert_called_once()
            assert mock_resolve.call_args.args[0] == "leo"

            # Verify safe_deliver received resolved session name
            mock_deliver.assert_called_once()
            assert mock_deliver.call_args.args[0] == "leo-session"
        # deliver_message patched via context manager

    async def test_send_directed_unresolvable(self, api_client, auth_headers, room_dir, api_app):
        """Unresolvable target returns unresolvable status."""
        mock_deliver = AsyncMock(return_value="delivered")
        with patch(_DELIVER_PATCH, mock_deliver):
            room = _make_room()
            _save_room_file(room_dir, room)

            with patch(
                "agent_backbone.api.routes.rooms._resolve_entity_session",
                return_value=None,
            ):
                resp = await api_client.post(
                    f"/api/rooms/{room.id}/directed",
                    headers=auth_headers,
                    json={"target": "leo", "content": "Hello"},
                )

            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is False
            assert data["status"] == "unresolvable"

            # safe_deliver never called
            mock_deliver.assert_not_called()

            # Message still in transcript
            saved = Room.model_validate_json((room_dir / f"{room.id}.json").read_text())
            assert len(saved.transcript) == 1
        # deliver_message patched via context manager


class TestMeetingSkillInjection:
    """Tests for meeting skill injection functions (ROOM-17, ROOM-18)."""

    def test_read_meeting_skill_success(self, tmp_path):
        """Reads existing skill file and returns content."""
        skill_file = tmp_path / "SKILL.md"
        skill_file.write_text("# Meeting Protocol\nBe concise.")

        with patch("agent_backbone.api.routes.rooms._MEETING_SKILL_PATH", skill_file):
            result = _read_meeting_skill()

        assert result == "# Meeting Protocol\nBe concise."

    def test_read_meeting_skill_missing(self, tmp_path):
        """Returns None and logs warning when skill file is missing."""
        missing = tmp_path / "nonexistent" / "SKILL.md"

        with patch("agent_backbone.api.routes.rooms._MEETING_SKILL_PATH", missing):
            result = _read_meeting_skill()

        assert result is None

    def test_format_meeting_setup_envelope(self):
        """Envelope matches ROOM-18 format with reply instructions."""
        room = _make_room(
            id="room-abc",
            title="Architecture review",
            moderator="ike",
            participants=["leo", "feynman"],
        )
        skill = "Be concise and constructive."

        result = _format_meeting_setup_envelope(room, skill)

        assert result.startswith("[via:meeting-setup room:room-abc]")
        assert 'You are joining meeting: "Architecture review"' in result
        assert "Moderator: ike" in result
        assert "Participants: leo, feynman" in result
        assert "--- Meeting Participant Protocol ---" in result
        assert "Be concise and constructive." in result
        assert "--- How to Reply ---" in result
        assert "room-respond.sh --room room-abc" in result
        assert result.endswith("---")

    def test_format_meeting_setup_envelope_single_participant(self):
        """Participant list renders correctly with a single participant."""
        room = _make_room(participants=["leo"])
        skill = "Protocol content"

        result = _format_meeting_setup_envelope(room, skill)

        assert "Participants: leo" in result

    async def test_inject_meeting_skill_delivers_to_all(self, room_dir):
        """Delivers skill to all participants via deliver_message."""
        room = _make_room(participants=["leo", "feynman"])
        config = MagicMock()
        mock_deliver = AsyncMock(return_value="delivered")

        with (
            patch(
                "agent_backbone.api.routes.rooms._read_meeting_skill",
                return_value="Skill content",
            ),
            patch(
                "agent_backbone.api.routes.rooms._resolve_entity_session",
                side_effect=lambda e, c: f"{e}-session",
            ),
            patch(_DELIVER_PATCH, mock_deliver),
        ):
            await _inject_meeting_skill(room, config)

        assert mock_deliver.call_count == 2
        sessions = [c[0][0] for c in mock_deliver.call_args_list]
        assert "leo-session" in sessions
        assert "feynman-session" in sessions
        # Verify envelope content
        envelope = mock_deliver.call_args_list[0][0][1]
        assert "[via:meeting-setup room:test-room-1]" in envelope
        assert "Skill content" in envelope

    async def test_inject_meeting_skill_missing_file_skips(self):
        """No delivery when skill file not found."""
        room = _make_room()
        config = MagicMock()
        mock_deliver = AsyncMock(return_value="delivered")

        with (
            patch(
                "agent_backbone.api.routes.rooms._read_meeting_skill",
                return_value=None,
            ),
            patch(_DELIVER_PATCH, mock_deliver),
        ):
            await _inject_meeting_skill(room, config)

        mock_deliver.assert_not_called()

    async def test_inject_meeting_skill_partial_failure_continues(self, room_dir):
        """One participant fails, others still get delivery."""
        room = _make_room(participants=["leo", "feynman"])
        config = MagicMock()
        mock_deliver = AsyncMock(side_effect=["offline", "delivered"])

        with (
            patch(
                "agent_backbone.api.routes.rooms._read_meeting_skill",
                return_value="Skill content",
            ),
            patch(
                "agent_backbone.api.routes.rooms._resolve_entity_session",
                side_effect=lambda e, c: f"{e}-session",
            ),
            patch(_DELIVER_PATCH, mock_deliver),
        ):
            await _inject_meeting_skill(room, config)

        # Both participants got delivery attempts
        assert mock_deliver.call_count == 2

    async def test_inject_meeting_skill_unresolvable_session_skips(self, room_dir):
        """Unresolvable session skips delivery for that participant."""
        room = _make_room(participants=["leo", "unknown"])
        config = MagicMock()
        mock_deliver = AsyncMock(return_value="delivered")

        with (
            patch(
                "agent_backbone.api.routes.rooms._read_meeting_skill",
                return_value="Skill content",
            ),
            patch(
                "agent_backbone.api.routes.rooms._resolve_entity_session",
                side_effect=lambda e, c: "leo-session" if e == "leo" else None,
            ),
            patch(_DELIVER_PATCH, mock_deliver),
        ):
            await _inject_meeting_skill(room, config)

        # Only the resolvable participant got delivery
        mock_deliver.assert_called_once()
        assert mock_deliver.call_args[0][0] == "leo-session"


class TestCursorAdvancement:
    """Tests for per-participant delivery cursor advancement."""

    async def test_directed_advances_cursor_on_delivery(
        self, api_client, auth_headers, room_dir, api_app
    ):
        """Successful directed delivery advances cursor; second delivery gets delta."""
        mock_deliver = AsyncMock(return_value="delivered")
        with patch(_DELIVER_PATCH, mock_deliver):
            room = _make_room(participants=["leo", "feynman"])
            _save_room_file(room_dir, room)

            with patch(
                "agent_backbone.api.routes.rooms._resolve_entity_session",
                return_value="leo-session",
            ):
                # First directed message
                resp = await api_client.post(
                    f"/api/rooms/{room.id}/directed",
                    headers=auth_headers,
                    json={"target": "leo", "content": "First question"},
                )
                assert resp.status_code == 200
                assert resp.json()["ok"] is True

            # Verify cursor was advanced
            saved = Room.model_validate_json((room_dir / f"{room.id}.json").read_text())
            assert saved.cursors.get("leo") == 1  # after 1 message
            assert "feynman" not in saved.cursors  # untouched

            # Second directed message — should only get delta (nothing new since cursor)
            with patch(
                "agent_backbone.api.routes.rooms._resolve_entity_session",
                return_value="leo-session",
            ):
                resp2 = await api_client.post(
                    f"/api/rooms/{room.id}/directed",
                    headers=auth_headers,
                    json={"target": "leo", "content": "Second question"},
                )
                assert resp2.status_code == 200

            saved2 = Room.model_validate_json((room_dir / f"{room.id}.json").read_text())
            assert saved2.cursors["leo"] == 2  # after 2 messages
        # deliver_message patched via context manager

    async def test_directed_does_not_advance_cursor_on_failure(
        self, api_client, auth_headers, room_dir, api_app
    ):
        """Failed delivery does not advance cursor."""
        mock_deliver = AsyncMock(return_value="offline")
        with patch(_DELIVER_PATCH, mock_deliver):
            room = _make_room(participants=["leo", "feynman"])
            _save_room_file(room_dir, room)

            with patch(
                "agent_backbone.api.routes.rooms._resolve_entity_session",
                return_value="leo-session",
            ):
                resp = await api_client.post(
                    f"/api/rooms/{room.id}/directed",
                    headers=auth_headers,
                    json={"target": "leo", "content": "Hello"},
                )
                assert resp.status_code == 200
                assert resp.json()["ok"] is False

            # Cursor should NOT have advanced
            saved = Room.model_validate_json((room_dir / f"{room.id}.json").read_text())
            assert "leo" not in saved.cursors
        # deliver_message patched via context manager

    async def test_broadcast_advances_cursors_per_participant(
        self, api_client, auth_headers, room_dir, api_app
    ):
        """Each participant's cursor advances independently based on delivery outcome."""
        mock_deliver = AsyncMock(side_effect=["delivered", "offline"])
        with patch(_DELIVER_PATCH, mock_deliver):
            room = _make_room(participants=["leo", "feynman"])
            _save_room_file(room_dir, room)

            with patch(
                "agent_backbone.api.routes.rooms._resolve_entity_session",
                side_effect=["leo-session", "feynman-session"],
            ):
                resp = await api_client.post(
                    f"/api/rooms/{room.id}/broadcast",
                    headers=auth_headers,
                    json={"content": "Discussion topic"},
                )

            assert resp.status_code == 200
            data = resp.json()
            assert data["delivered"] == 1
            assert data["failed"] == 1

            saved = Room.model_validate_json((room_dir / f"{room.id}.json").read_text())
            assert saved.cursors.get("leo") == 1  # delivered → advanced
            assert "feynman" not in saved.cursors  # failed → not advanced
        # deliver_message patched via context manager

    async def test_skill_injection_sets_initial_cursors(self):
        """After successful injection, cursors are set to transcript length."""
        msg1 = _make_message(id="m1")
        room = _make_room(participants=["leo", "feynman"], transcript=[msg1])
        config = MagicMock()
        mock_deliver = AsyncMock(return_value="delivered")

        with (
            patch(
                "agent_backbone.api.routes.rooms._read_meeting_skill",
                return_value="Skill content",
            ),
            patch(
                "agent_backbone.api.routes.rooms._resolve_entity_session",
                side_effect=lambda e, c: f"{e}-session",
            ),
            patch(_DELIVER_PATCH, mock_deliver),
            patch(
                "agent_backbone.api.routes.rooms._save_room",
                new_callable=AsyncMock,
            ) as mock_save,
        ):
            await _inject_meeting_skill(room, config)

        # Both participants delivered successfully -> cursors set to transcript length
        assert room.cursors["leo"] == 1
        assert room.cursors["feynman"] == 1
        mock_save.assert_called_once_with(room)

    async def test_skill_injection_partial_sets_cursor_only_for_delivered(self):
        """Only successfully injected participants get their cursor set."""
        room = _make_room(participants=["leo", "feynman"])
        config = MagicMock()
        mock_deliver = AsyncMock(side_effect=["delivered", "offline"])

        with (
            patch(
                "agent_backbone.api.routes.rooms._read_meeting_skill",
                return_value="Skill content",
            ),
            patch(
                "agent_backbone.api.routes.rooms._resolve_entity_session",
                side_effect=lambda e, c: f"{e}-session",
            ),
            patch(_DELIVER_PATCH, mock_deliver),
            patch(
                "agent_backbone.api.routes.rooms._save_room",
                new_callable=AsyncMock,
            ) as mock_save,
        ):
            await _inject_meeting_skill(room, config)

        assert room.cursors.get("leo") == 0  # empty transcript -> len=0
        assert "feynman" not in room.cursors  # failed -> no cursor
        mock_save.assert_called_once()

    def test_existing_room_without_cursors_field(self):
        """Backward compat: room JSON without 'cursors' deserializes with empty dict."""
        room_data = {
            "id": "old-room",
            "title": "Legacy room",
            "moderator": "ike",
            "participants": ["leo"],
            "state": "active",
            "transcript": [],
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
        room = Room.model_validate(room_data)
        assert room.cursors == {}

        # Delta computation works with missing cursors (defaults to 0)
        delta = _compute_context_delta(room, "leo")
        assert delta == []
