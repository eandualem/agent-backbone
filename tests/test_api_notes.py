"""Tests for api/routes/notes.py — notes CRUD endpoints."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient


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
    with patch("api.routes.notes._NOTES_ROOT", notes_dir):
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
# POST /api/notes
# ---------------------------------------------------------------------------


class TestCreateNote:
    async def test_creates_note(self, client, auth_headers, notes_tmp):
        """Creates a markdown note with slugified filename."""
        resp = await client.post(
            "/api/notes",
            json={"title": "My New Note", "content": "# My New Note\nHello world"},
            headers=auth_headers,
        )

        assert resp.status_code == 201
        data = resp.json()
        assert data["title"] == "My New Note"
        assert data["content"] == "# My New Note\nHello world"
        assert data["id"].endswith(".md")

        # Verify file on disk
        file_path = notes_tmp / data["id"]
        assert file_path.exists()
        assert file_path.read_text() == "# My New Note\nHello world"

    async def test_creates_in_subdir(self, client, auth_headers, notes_tmp):
        """Creates a note in a subdirectory."""
        resp = await client.post(
            "/api/notes",
            json={
                "title": "Sub Note",
                "content": "# Sub Note\nContent",
                "subdir": "projects",
            },
            headers=auth_headers,
        )

        assert resp.status_code == 201
        assert "projects/" in resp.json()["id"]
        assert (notes_tmp / "projects").is_dir()

    async def test_avoids_name_collision(self, client, auth_headers, notes_tmp):
        """Appends counter to avoid overwriting existing files."""
        (notes_tmp / "my-note.md").write_text("existing")

        resp = await client.post(
            "/api/notes",
            json={"title": "My Note", "content": "new content"},
            headers=auth_headers,
        )

        assert resp.status_code == 201
        assert resp.json()["id"] == "my-note-1.md"

    async def test_subdir_traversal_rejected(self, client, auth_headers, notes_tmp):
        """Path traversal via subdir in create is rejected."""
        resp = await client.post(
            "/api/notes",
            json={
                "title": "Evil",
                "content": "hacked",
                "subdir": "../../etc",
            },
            headers=auth_headers,
        )

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
# PUT /api/notes/{note_id}
# ---------------------------------------------------------------------------


class TestUpdateNote:
    async def test_updates_note_content(self, client, auth_headers, notes_tmp):
        """Updates note content and returns updated detail."""
        (notes_tmp / "update-me.md").write_text("# Old Title\nOld content")

        resp = await client.put(
            "/api/notes/update-me.md",
            json={"content": "# New Title\nNew content"},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "New Title"
        assert data["content"] == "# New Title\nNew content"

        # Verify on disk
        assert (notes_tmp / "update-me.md").read_text() == "# New Title\nNew content"

    async def test_update_nonexistent_returns_404(self, client, auth_headers, notes_tmp):
        """Returns 404 when updating a note that doesn't exist."""
        resp = await client.put(
            "/api/notes/ghost.md",
            json={"content": "doesn't matter"},
            headers=auth_headers,
        )

        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/notes/{note_id}
# ---------------------------------------------------------------------------


class TestDeleteNote:
    async def test_deletes_note(self, client, auth_headers, notes_tmp):
        """Deletes a note file and returns confirmation."""
        (notes_tmp / "delete-me.md").write_text("# Goodbye")

        resp = await client.delete("/api/notes/delete-me.md", headers=auth_headers)

        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        assert not (notes_tmp / "delete-me.md").exists()

    async def test_delete_nonexistent_returns_404(self, client, auth_headers, notes_tmp):
        """Returns 404 when deleting a note that doesn't exist."""
        resp = await client.delete("/api/notes/ghost.md", headers=auth_headers)

        assert resp.status_code == 404
