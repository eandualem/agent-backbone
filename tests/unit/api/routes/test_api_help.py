"""Tests for the help and docs routes at api/routes/help.py."""

from __future__ import annotations


class TestHelpRoutes:
    async def test_topics_index_and_page(self, api_client, auth_headers):
        resp = await api_client.get("/api/help", headers=auth_headers)
        assert resp.status_code == 200
        assert "setup" in {t["name"] for t in resp.json()["items"]}

        resp = await api_client.get("/api/help/setup", headers=auth_headers)
        assert resp.status_code == 200
        assert "backbone init" in resp.json()["content"]

    async def test_unknown_topic_lists_the_known_ones(self, api_client, auth_headers):
        resp = await api_client.get("/api/help/nope", headers=auth_headers)
        assert resp.status_code == 404
        assert "swarms" in resp.json()["detail"]


class TestDocsRoutes:
    async def test_docs_index_and_page(self, api_client, auth_headers):
        resp = await api_client.get("/api/docs", headers=auth_headers)
        assert resp.status_code == 200
        assert "getting-started" in {p["name"] for p in resp.json()["items"]}

        resp = await api_client.get("/api/docs/concepts", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["content"].startswith("# Concepts")

    async def test_unknown_page_is_404(self, api_client, auth_headers):
        resp = await api_client.get("/api/docs/nope", headers=auth_headers)
        assert resp.status_code == 404
        assert "getting-started" in resp.json()["detail"]

    async def test_docs_need_the_key(self, api_client):
        resp = await api_client.get("/api/docs")
        assert resp.status_code == 401
