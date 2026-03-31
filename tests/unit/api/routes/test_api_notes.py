"""Tests for api/routes/notes.py — notes CRUD endpoints."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agent_backbone.api.routes import files as files_routes


@pytest.fixture
async def client(api_app):
    """Async test client bound to the api app."""
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def notes_tmp(tmp_path):
    """Patch _NOTES_ROOT to use tmp_path for test isolation."""
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    with patch("agent_backbone.api.routes.files._NOTES_ROOT", notes_dir):
        yield notes_dir


# ---------------------------------------------------------------------------
# GET /api/notes
# ---------------------------------------------------------------------------


class TestListNotes:
    async def test_returns_notes_list(self, client, auth_headers, notes_tmp):
        """Returns markdown notes found in the notes directory."""
        (notes_tmp / "first.md").write_text("# First Note\nSome content here")
        (notes_tmp / "second.md").write_text("# Second Note\nMore content")

        resp = await client.get("/api/notes", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        titles = [item["title"] for item in data["items"]]
        assert "First Note" in titles
        assert "Second Note" in titles

    async def test_returns_notes_from_subdir(self, client, auth_headers, notes_tmp):
        """Returns notes from a specific subdirectory."""
        sub = notes_tmp / "work"
        sub.mkdir()
        (sub / "task.md").write_text("# Work Task\nDo stuff")

        resp = await client.get("/api/notes?subdir=work", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["title"] == "Work Task"

    async def test_empty_when_no_notes(self, client, auth_headers, notes_tmp):
        """Returns empty list when notes directory has no markdown files."""
        resp = await client.get("/api/notes", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0

    async def test_path_traversal_rejected(self, client, auth_headers, notes_tmp):
        """Path traversal via subdir is rejected."""
        resp = await client.get("/api/notes?subdir=../../etc", headers=auth_headers)

        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET /api/notes/{note_id}
# ---------------------------------------------------------------------------


class TestGetNote:
    async def test_returns_note_content(self, client, auth_headers, notes_tmp):
        """Returns full note content by ID."""
        (notes_tmp / "test.md").write_text("# Test\nFull content here")

        resp = await client.get("/api/notes/test.md", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "test.md"
        assert data["title"] == "Test"
        assert "Full content here" in data["content"]

    async def test_nonexistent_returns_404(self, client, auth_headers, notes_tmp):
        """Returns 404 for nonexistent note."""
        resp = await client.get("/api/notes/ghost.md", headers=auth_headers)

        assert resp.status_code == 404

    async def test_path_traversal_rejected(self, client, auth_headers, notes_tmp):
        """Path traversal in note ID is rejected."""
        resp = await client.get("/api/notes/../../etc/passwd", headers=auth_headers)

        assert resp.status_code in (403, 404)


# ---------------------------------------------------------------------------
# POST /api/notes
# ---------------------------------------------------------------------------


class TestCreateNote:
    async def test_creates_note(self, client, auth_headers, notes_tmp):
        """Creates a new markdown note and returns NoteDetail."""
        resp = await client.post(
            "/api/notes",
            json={"title": "My Note", "content": "# My Note\nHello world"},
            headers=auth_headers,
        )

        assert resp.status_code == 201
        data = resp.json()
        assert data["title"] == "My Note"
        assert "Hello world" in data["content"]
        assert data["id"] == "my-note.md"
        # File actually exists on disk
        assert (notes_tmp / "my-note.md").is_file()

    async def test_creates_note_in_subdir(self, client, auth_headers, notes_tmp):
        """Creates note in a subdirectory."""
        resp = await client.post(
            "/api/notes",
            json={"title": "Sub Note", "content": "content", "subdir": "work"},
            headers=auth_headers,
        )

        assert resp.status_code == 201
        assert (notes_tmp / "work" / "sub-note.md").is_file()

    async def test_path_traversal_rejected(self, client, auth_headers, notes_tmp):
        """Path traversal via subdir is rejected."""
        resp = await client.post(
            "/api/notes",
            json={"title": "Evil", "content": "x", "subdir": "../../etc"},
            headers=auth_headers,
        )

        assert resp.status_code == 403

    async def test_empty_title_rejected(self, client, auth_headers, notes_tmp):
        """Title with no valid filename chars is rejected."""
        resp = await client.post(
            "/api/notes",
            json={"title": "!!!", "content": "x"},
            headers=auth_headers,
        )

        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# PUT /api/notes/{note_id}
# ---------------------------------------------------------------------------


class TestUpdateNote:
    async def test_updates_note(self, client, auth_headers, notes_tmp):
        """Updates an existing note's content."""
        (notes_tmp / "edit-me.md").write_text("# Old\nOld content")

        resp = await client.put(
            "/api/notes/edit-me.md",
            json={"content": "# Updated\nNew content"},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Updated"
        assert "New content" in data["content"]
        assert (notes_tmp / "edit-me.md").read_text() == "# Updated\nNew content"

    async def test_nonexistent_returns_404(self, client, auth_headers, notes_tmp):
        """Returns 404 for nonexistent note."""
        resp = await client.put(
            "/api/notes/ghost.md",
            json={"content": "x"},
            headers=auth_headers,
        )

        assert resp.status_code == 404

    async def test_path_traversal_rejected(self, client, auth_headers, notes_tmp):
        """Path traversal in note ID is rejected."""
        resp = await client.put(
            "/api/notes/../../etc/passwd",
            json={"content": "x"},
            headers=auth_headers,
        )

        assert resp.status_code in (403, 404)


# ---------------------------------------------------------------------------
# DELETE /api/notes/{note_id}
# ---------------------------------------------------------------------------


class TestDeleteNote:
    async def test_deletes_note(self, client, auth_headers, notes_tmp):
        """Deletes an existing note."""
        (notes_tmp / "delete-me.md").write_text("# Delete Me")

        resp = await client.delete("/api/notes/delete-me.md", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] is True
        assert data["id"] == "delete-me.md"
        assert not (notes_tmp / "delete-me.md").exists()

    async def test_nonexistent_returns_404(self, client, auth_headers, notes_tmp):
        """Returns 404 for nonexistent note."""
        resp = await client.delete("/api/notes/ghost.md", headers=auth_headers)

        assert resp.status_code == 404

    async def test_path_traversal_rejected(self, client, auth_headers, notes_tmp):
        """Path traversal in note ID is rejected."""
        resp = await client.delete("/api/notes/../../etc/passwd", headers=auth_headers)

        assert resp.status_code in (403, 404)


class TestNotesUseToThread:
    async def test_list_notes_uses_to_thread(self, client, auth_headers, notes_tmp):
        (notes_tmp / "threaded.md").write_text("# Threaded\ncontent")

        with patch(
            "agent_backbone.api.routes.files.asyncio.to_thread",
            side_effect=lambda func, *args: func(*args),
        ) as mock_to_thread:
            resp = await client.get("/api/notes", headers=auth_headers)

        assert resp.status_code == 200
        mock_to_thread.assert_awaited_once()
        assert mock_to_thread.await_args.args[0] is files_routes._list_notes_sync

    async def test_get_note_uses_to_thread(self, client, auth_headers, notes_tmp):
        (notes_tmp / "threaded.md").write_text("# Threaded\ncontent")

        with patch(
            "agent_backbone.api.routes.files.asyncio.to_thread",
            side_effect=lambda func, *args: func(*args),
        ) as mock_to_thread:
            resp = await client.get("/api/notes/threaded.md", headers=auth_headers)

        assert resp.status_code == 200
        mock_to_thread.assert_awaited_once()
        assert mock_to_thread.await_args.args[0] is files_routes._read_note_sync

    async def test_create_note_uses_to_thread(self, client, auth_headers, notes_tmp):
        with patch(
            "agent_backbone.api.routes.files.asyncio.to_thread",
            side_effect=lambda func, *args: func(*args),
        ) as mock_to_thread:
            resp = await client.post(
                "/api/notes",
                json={"title": "Threaded Create", "content": "# Threaded Create\ncontent"},
                headers=auth_headers,
            )

        assert resp.status_code == 201
        mock_to_thread.assert_awaited_once()
        assert mock_to_thread.await_args.args[0] is files_routes._create_note_sync

    async def test_update_note_uses_to_thread(self, client, auth_headers, notes_tmp):
        (notes_tmp / "threaded.md").write_text("# Old\ncontent")

        with patch(
            "agent_backbone.api.routes.files.asyncio.to_thread",
            side_effect=lambda func, *args: func(*args),
        ) as mock_to_thread:
            resp = await client.put(
                "/api/notes/threaded.md",
                json={"content": "# Updated\ncontent"},
                headers=auth_headers,
            )

        assert resp.status_code == 200
        mock_to_thread.assert_awaited_once()
        assert mock_to_thread.await_args.args[0] is files_routes._update_note_sync

    async def test_delete_note_uses_to_thread(self, client, auth_headers, notes_tmp):
        (notes_tmp / "threaded.md").write_text("# Threaded\ncontent")

        with patch(
            "agent_backbone.api.routes.files.asyncio.to_thread",
            side_effect=lambda func, *args: func(*args),
        ) as mock_to_thread:
            resp = await client.delete("/api/notes/threaded.md", headers=auth_headers)

        assert resp.status_code == 200
        mock_to_thread.assert_awaited_once()
        assert mock_to_thread.await_args.args[0] is files_routes._delete_note_sync
