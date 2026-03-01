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
