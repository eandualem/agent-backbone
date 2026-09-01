"""Tests for api/auth.py bearer token authentication."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from agent_backbone.api.auth import require_api_key
from agent_backbone.config import SecurityConfig


@pytest.fixture
def auth_app(config) -> FastAPI:
    """Minimal app protected by the API key dependency."""
    app = FastAPI()
    app.state.config = replace(config, api_key="secret-key")

    @app.get("/protected", dependencies=[Depends(require_api_key)])
    async def protected() -> dict[str, bool]:
        return {"ok": True}

    return app


@pytest.fixture
async def auth_client(auth_app: FastAPI):
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


class TestRequireApiKey:
    async def test_no_key_configured_is_rejected(self, auth_client, auth_app):
        auth_app.state.config = replace(auth_app.state.config, api_key="")

        resp = await auth_client.get("/protected")

        assert resp.status_code == 401
        assert "No API key configured" in resp.json()["detail"]

    async def test_no_key_allowed_when_explicitly_unauthenticated(self, auth_client, auth_app):
        auth_app.state.config = replace(
            auth_app.state.config,
            api_key="",
            security=SecurityConfig(allow_unauthenticated=True),
        )

        resp = await auth_client.get("/protected")

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    async def test_auth_valid_bearer(self, auth_client):
        resp = await auth_client.get("/protected", headers={"Authorization": "Bearer secret-key"})

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    async def test_auth_invalid_bearer(self, auth_client):
        resp = await auth_client.get("/protected", headers={"Authorization": "Bearer wrong-key"})

        assert resp.status_code == 401
        assert resp.json() == {"detail": "Invalid or missing API key"}

    async def test_auth_missing_bearer(self, auth_client):
        resp = await auth_client.get("/protected")

        assert resp.status_code == 401
        assert resp.json() == {"detail": "Invalid or missing API key"}

    async def test_auth_uses_compare_digest(self, auth_client):
        with patch("agent_backbone.api.auth.hmac.compare_digest", return_value=True) as compare:
            resp = await auth_client.get(
                "/protected", headers={"Authorization": "Bearer secret-key"}
            )

        assert resp.status_code == 200
        compare.assert_called_once_with("secret-key", "secret-key")
