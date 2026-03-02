"""Tests for api/routes/rooms.py — room management endpoints."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.deps import get_delivery_service
from api.models import Room, RoomMessage
from api.routes.rooms import _compute_context_delta, _format_room_message


@pytest.fixture
def room_dir(tmp_path):
    """Patch _ROOM_DIR to use tmp_path for isolation."""
    d = tmp_path / "rooms"
    d.mkdir()
    with patch("api.routes.rooms._ROOM_DIR", d):
        yield d


def _make_mock_delivery_svc(safe_deliver_return="delivered", safe_deliver_side_effect=None):
    """Create a mock DeliveryService with safe_deliver configured."""
    svc = MagicMock()
    svc.safe_deliver = AsyncMock(
        return_value=safe_deliver_return,
        side_effect=safe_deliver_side_effect,
    )
    return svc


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

    async def test_create_room_success(self, api_client, auth_headers, room_dir):
        """201 with UUID id, state=active, empty transcript, file persisted."""
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
        mock_svc = _make_mock_delivery_svc()
        api_app.dependency_overrides[get_delivery_service] = lambda: mock_svc
        try:
            room = _make_room()
            _save_room_file(room_dir, room)

            with patch(
                "api.routes.rooms.resolve_entity_session",
                new_callable=AsyncMock,
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

            # Verify safe_deliver was called
            mock_svc.safe_deliver.assert_called_once()
            call_args = mock_svc.safe_deliver.call_args
            assert call_args[0][0] == "leo"
            assert "What's your take?" in call_args[0][1]

            # Verify message in transcript
            saved = Room.model_validate_json((room_dir / f"{room.id}.json").read_text())
            assert len(saved.transcript) == 1
            assert saved.transcript[0].mode == "directed"
            assert saved.transcript[0].sender == "ike"
        finally:
            api_app.dependency_overrides.pop(get_delivery_service, None)

    async def test_send_directed_target_not_participant(
        self, api_client, auth_headers, room_dir, api_app
    ):
        """400 when target is not a room participant."""
        mock_svc = _make_mock_delivery_svc()
        api_app.dependency_overrides[get_delivery_service] = lambda: mock_svc
        try:
            room = _make_room(participants=["leo", "feynman"])
            _save_room_file(room_dir, room)

            resp = await api_client.post(
                f"/api/rooms/{room.id}/directed",
                headers=auth_headers,
                json={"target": "ada", "content": "Hello"},
            )

            assert resp.status_code == 400
            assert "not a room participant" in resp.json()["detail"]
            mock_svc.safe_deliver.assert_not_called()
        finally:
            api_app.dependency_overrides.pop(get_delivery_service, None)

    async def test_send_directed_room_not_found(self, api_client, auth_headers, room_dir, api_app):
        """404 for non-existent room."""
        mock_svc = _make_mock_delivery_svc()
        api_app.dependency_overrides[get_delivery_service] = lambda: mock_svc
        try:
            resp = await api_client.post(
                "/api/rooms/no-room/directed",
                headers=auth_headers,
                json={"target": "leo", "content": "Hello"},
            )
            assert resp.status_code == 404
        finally:
            api_app.dependency_overrides.pop(get_delivery_service, None)


class TestSendBroadcast:
    """Tests for POST /api/rooms/{room_id}/broadcast."""

    async def test_send_broadcast_success(self, api_client, auth_headers, room_dir, api_app):
        """200, all delivered, message appended with mode=broadcast."""
        mock_svc = _make_mock_delivery_svc()
        api_app.dependency_overrides[get_delivery_service] = lambda: mock_svc
        try:
            room = _make_room(participants=["leo", "feynman"])
            _save_room_file(room_dir, room)

            with patch(
                "api.routes.rooms.resolve_entity_session",
                new_callable=AsyncMock,
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

            # Verify safe_deliver called for each participant
            assert mock_svc.safe_deliver.call_count == 2

            # Verify transcript
            saved = Room.model_validate_json((room_dir / f"{room.id}.json").read_text())
            assert len(saved.transcript) == 1
            assert saved.transcript[0].mode == "broadcast"
            assert set(saved.transcript[0].recipients) == {"leo", "feynman"}
        finally:
            api_app.dependency_overrides.pop(get_delivery_service, None)

    async def test_send_broadcast_partial_failure(
        self, api_client, auth_headers, room_dir, api_app
    ):
        """Partial delivery: delivered=1, failed=1, message still appended."""
        mock_svc = _make_mock_delivery_svc(safe_deliver_side_effect=["delivered", "offline"])
        api_app.dependency_overrides[get_delivery_service] = lambda: mock_svc
        try:
            room = _make_room(participants=["leo", "feynman"])
            _save_room_file(room_dir, room)

            with patch(
                "api.routes.rooms.resolve_entity_session",
                new_callable=AsyncMock,
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
        finally:
            api_app.dependency_overrides.pop(get_delivery_service, None)

    async def test_send_broadcast_room_not_found(self, api_client, auth_headers, room_dir, api_app):
        """404 for non-existent room."""
        mock_svc = _make_mock_delivery_svc()
        api_app.dependency_overrides[get_delivery_service] = lambda: mock_svc
        try:
            resp = await api_client.post(
                "/api/rooms/no-room/broadcast",
                headers=auth_headers,
                json={"content": "Hello all"},
            )
            assert resp.status_code == 404
        finally:
            api_app.dependency_overrides.pop(get_delivery_service, None)


class TestPostResponse:
    """Tests for POST /api/rooms/{room_id}/respond."""

    async def test_post_response_success(self, api_client, auth_headers, room_dir):
        """200, message appended with mode=response."""
        room = _make_room()
        _save_room_file(room_dir, room)

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

    async def test_post_response_room_not_found(self, api_client, auth_headers, room_dir):
        """404 for non-existent room."""
        resp = await api_client.post(
            "/api/rooms/no-room/respond",
            headers=auth_headers,
            json={"sender": "leo", "content": "Hello"},
        )
        assert resp.status_code == 404


class TestUpdateRoomState:
    """Tests for PATCH /api/rooms/{room_id}."""

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


class TestContextDelta:
    """Tests for _compute_context_delta logic."""

    def test_first_participation_returns_all(self):
        """Returns all messages when participant never responded."""
        msg1 = _make_message(id="m1", sender="ike", content="Question 1")
        msg2 = _make_message(id="m2", sender="ike", content="Question 2")
        room = _make_room(transcript=[msg1, msg2])

        delta = _compute_context_delta(room, "leo")

        assert len(delta) == 2
        assert delta[0].id == "m1"
        assert delta[1].id == "m2"

    def test_after_response_returns_subsequent(self):
        """Returns only messages after participant's last response."""
        msg1 = _make_message(id="m1", sender="ike", content="Q1")
        response = _make_message(
            id="m2", sender="leo", mode="response", content="A1", recipients=[]
        )
        msg3 = _make_message(id="m3", sender="ike", content="Q2")
        msg4 = _make_message(id="m4", sender="feynman", mode="response", content="A2")
        room = _make_room(transcript=[msg1, response, msg3, msg4])

        delta = _compute_context_delta(room, "leo")

        assert len(delta) == 2
        assert delta[0].id == "m3"
        assert delta[1].id == "m4"

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
        mock_svc = _make_mock_delivery_svc()
        api_app.dependency_overrides[get_delivery_service] = lambda: mock_svc
        try:
            room = _make_room(participants=["leo", "feynman"])
            _save_room_file(room_dir, room)

            with patch(
                "api.routes.rooms.resolve_entity_session",
                new_callable=AsyncMock,
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
            deliver_sessions = [c.args[0] for c in mock_svc.safe_deliver.call_args_list]
            assert "leo-session" in deliver_sessions
            assert "feynman-session" in deliver_sessions
        finally:
            api_app.dependency_overrides.pop(get_delivery_service, None)

    async def test_send_broadcast_unresolvable_participant(
        self, api_client, auth_headers, room_dir, api_app
    ):
        """Unresolvable participant counted as failed, not delivered."""
        mock_svc = _make_mock_delivery_svc()
        api_app.dependency_overrides[get_delivery_service] = lambda: mock_svc
        try:
            room = _make_room(participants=["leo", "unknown-entity"])
            _save_room_file(room_dir, room)

            with patch(
                "api.routes.rooms.resolve_entity_session",
                new_callable=AsyncMock,
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
            assert mock_svc.safe_deliver.call_count == 1
        finally:
            api_app.dependency_overrides.pop(get_delivery_service, None)

    async def test_send_broadcast_exception_logged(
        self, api_client, auth_headers, room_dir, api_app
    ):
        """Exceptions from gather are caught; counted as failed."""
        mock_svc = _make_mock_delivery_svc()
        api_app.dependency_overrides[get_delivery_service] = lambda: mock_svc
        try:
            room = _make_room(participants=["leo", "feynman"])
            _save_room_file(room_dir, room)

            # First participant resolves fine, second raises
            async def _resolve_side_effect(target, config):
                if target == "leo":
                    return "leo-session"
                raise RuntimeError("tmux error")

            with patch(
                "api.routes.rooms.resolve_entity_session",
                new_callable=AsyncMock,
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
        finally:
            api_app.dependency_overrides.pop(get_delivery_service, None)


class TestDirectedSessionResolution:
    """Tests for entity->session resolution in directed delivery."""

    async def test_send_directed_resolves_session(
        self, api_client, auth_headers, room_dir, api_app
    ):
        """resolve_entity_session called; resolved name passed to safe_deliver."""
        mock_svc = _make_mock_delivery_svc()
        api_app.dependency_overrides[get_delivery_service] = lambda: mock_svc
        try:
            room = _make_room()
            _save_room_file(room_dir, room)

            with patch(
                "api.routes.rooms.resolve_entity_session",
                new_callable=AsyncMock,
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
            mock_svc.safe_deliver.assert_called_once()
            assert mock_svc.safe_deliver.call_args.args[0] == "leo-session"
        finally:
            api_app.dependency_overrides.pop(get_delivery_service, None)

    async def test_send_directed_unresolvable(self, api_client, auth_headers, room_dir, api_app):
        """Unresolvable target returns unresolvable status."""
        mock_svc = _make_mock_delivery_svc()
        api_app.dependency_overrides[get_delivery_service] = lambda: mock_svc
        try:
            room = _make_room()
            _save_room_file(room_dir, room)

            with patch(
                "api.routes.rooms.resolve_entity_session",
                new_callable=AsyncMock,
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
            mock_svc.safe_deliver.assert_not_called()

            # Message still in transcript
            saved = Room.model_validate_json((room_dir / f"{room.id}.json").read_text())
            assert len(saved.transcript) == 1
        finally:
            api_app.dependency_overrides.pop(get_delivery_service, None)
