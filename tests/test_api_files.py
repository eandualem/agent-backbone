"""Tests for api/routes/files.py — restricted file browsing, reading, and writing endpoints."""

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
def allowed_tmp(tmp_path):
    """Patch allowed prefixes to include tmp_path for test isolation."""
    with patch("api.routes.files._ALLOWED_PREFIXES", [tmp_path]):
        yield tmp_path


# ---------------------------------------------------------------------------
# GET /api/files/tree
# ---------------------------------------------------------------------------


class TestGetFileTree:
    async def test_returns_directory_tree(self, client, auth_headers, allowed_tmp):
        """Returns files and directories under the requested path."""
        # Create a directory structure
        sub = allowed_tmp / "subdir"
        sub.mkdir()
        (allowed_tmp / "README.md").write_text("hello")
        (sub / "config.toml").write_text("[section]")

        resp = await client.get(
            f"/api/files/tree?path={allowed_tmp}&depth=2",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        data = resp.json()
        names = [node["name"] for node in data]
        # Directory sorted first, then file
        assert "subdir" in names
        assert "README.md" in names

        # Subdirectory should contain its child
        subdir_node = next(n for n in data if n["name"] == "subdir")
        assert subdir_node["type"] == "directory"
        assert subdir_node["children"] is not None
        child_names = [c["name"] for c in subdir_node["children"]]
        assert "config.toml" in child_names

    async def test_forbidden_path_returns_403(self, client, auth_headers, allowed_tmp):
        """Returns 403 when path is outside allowed prefixes."""
        resp = await client.get(
            "/api/files/tree?path=/etc",
            headers=auth_headers,
        )

        assert resp.status_code == 403
        assert "not in allowed" in resp.json()["detail"].lower()

    async def test_nonexistent_directory_returns_404(self, client, auth_headers, allowed_tmp):
        """Returns 404 when the path does not exist."""
        ghost_path = allowed_tmp / "nonexistent"

        resp = await client.get(
            f"/api/files/tree?path={ghost_path}",
            headers=auth_headers,
        )

        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    async def test_hidden_files_excluded(self, client, auth_headers, allowed_tmp):
        """Files starting with dot are excluded from the tree."""
        (allowed_tmp / ".hidden").write_text("secret")
        (allowed_tmp / "visible.txt").write_text("public")

        resp = await client.get(
            f"/api/files/tree?path={allowed_tmp}&depth=1",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        data = resp.json()
        names = [node["name"] for node in data]
        assert "visible.txt" in names
        assert ".hidden" not in names

    async def test_requires_auth(self, client, api_key, allowed_tmp):
        """Request without auth headers is rejected."""
        resp = await client.get(f"/api/files/tree?path={allowed_tmp}")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/files/content
# ---------------------------------------------------------------------------


class TestGetFileContent:
    async def test_returns_file_content(self, client, auth_headers, allowed_tmp):
        """Returns the text content of a readable file."""
        target = allowed_tmp / "example.txt"
        target.write_text("Hello, backbone!")

        resp = await client.get(
            f"/api/files/content?path={target}",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["content"] == "Hello, backbone!"
        assert str(target) in data["path"]

    async def test_forbidden_path_returns_403(self, client, auth_headers, allowed_tmp):
        """Returns 403 when the file is outside allowed prefixes."""
        resp = await client.get(
            "/api/files/content?path=/etc/passwd",
            headers=auth_headers,
        )

        assert resp.status_code == 403
        assert "not in allowed" in resp.json()["detail"].lower()

    async def test_nonexistent_file_returns_404(self, client, auth_headers, allowed_tmp):
        """Returns 404 when the file does not exist."""
        ghost = allowed_tmp / "ghost.txt"

        resp = await client.get(
            f"/api/files/content?path={ghost}",
            headers=auth_headers,
        )

        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# POST /api/files/content
# ---------------------------------------------------------------------------


class TestWriteFileContent:
    async def test_writes_file(self, client, auth_headers, allowed_tmp):
        """Writes content to a file under allowed path."""
        target = allowed_tmp / "new-file.txt"

        resp = await client.post(
            "/api/files/content",
            json={"path": str(target), "content": "Hello from API"},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["written"] is True
        assert target.read_text() == "Hello from API"

    async def test_creates_parent_directories(self, client, auth_headers, allowed_tmp):
        """Creates parent directories if they don't exist."""
        target = allowed_tmp / "sub" / "deep" / "file.txt"

        resp = await client.post(
            "/api/files/content",
            json={"path": str(target), "content": "nested content"},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert target.read_text() == "nested content"

    async def test_forbidden_path_returns_403(self, client, auth_headers, allowed_tmp):
        """Returns 403 when path is outside allowed prefixes."""
        resp = await client.post(
            "/api/files/content",
            json={"path": "/etc/evil.txt", "content": "nope"},
            headers=auth_headers,
        )

        assert resp.status_code == 403
        assert "not in allowed" in resp.json()["detail"].lower()

    async def test_overwrites_existing_file(self, client, auth_headers, allowed_tmp):
        """Overwrites existing file content."""
        target = allowed_tmp / "existing.txt"
        target.write_text("old content")

        resp = await client.post(
            "/api/files/content",
            json={"path": str(target), "content": "new content"},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert target.read_text() == "new content"
