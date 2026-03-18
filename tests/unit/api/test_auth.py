"""Tests for api/auth.py bearer token authentication."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from agent_backbone.api.auth import require_api_key


@pytest.fixture
def auth_app() -> FastAPI:
    """Create a minimal app protected by the API key dependency."""
    app = FastAPI()

    @app.get("/protected", dependencies=[Depends(require_api_key)])
    async def protected() -> dict[str, bool]:
        return {"ok": True}

    return app


@pytest.fixture
async def auth_client(auth_app: FastAPI):
    """Create an async client for the minimal auth test app."""
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


class TestRequireApiKey:
    async def test_auth_dev_mode_bypass(self, auth_client, monkeypatch):
        monkeypatch.delenv("BACKBONE_API_KEY", raising=False)

        resp = await auth_client.get("/protected")

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    async def test_auth_valid_bearer(self, auth_client, monkeypatch):
        monkeypatch.setenv("BACKBONE_API_KEY", "secret-key")

        resp = await auth_client.get(
            "/protected",
            headers={"Authorization": "Bearer secret-key"},
        )

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    async def test_auth_invalid_bearer(self, auth_client, monkeypatch):
        monkeypatch.setenv("BACKBONE_API_KEY", "secret-key")

        resp = await auth_client.get(
            "/protected",
            headers={"Authorization": "Bearer wrong-key"},
        )

        assert resp.status_code == 401
        assert resp.json() == {"detail": "Invalid or missing API key"}

    async def test_auth_missing_bearer(self, auth_client, monkeypatch):
        monkeypatch.setenv("BACKBONE_API_KEY", "secret-key")

        resp = await auth_client.get("/protected")

        assert resp.status_code == 401
        assert resp.json() == {"detail": "Invalid or missing API key"}

    async def test_auth_uses_compare_digest(self, auth_client, monkeypatch):
        monkeypatch.setenv("BACKBONE_API_KEY", "secret-key")

        with patch("agent_backbone.api.auth.hmac.compare_digest", return_value=True) as compare:
            resp = await auth_client.get(
                "/protected",
                headers={"Authorization": "Bearer secret-key"},
            )

        assert resp.status_code == 200
        compare.assert_called_once_with("secret-key", "secret-key")
