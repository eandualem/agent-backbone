"""One durable publication/read contract, independent of the author's CLI."""

import hashlib
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_backbone.models import DOCUMENT_HTML_BYTES, DOCUMENT_WIRE_BYTES
from tests.report_support import author
from tests.unit.api.routes.test_api_signing import NOW, SENDER, _enroll, _signed


def body(author="ike", key="first", supersedes_id=None, html="<h1>Exact ☃</h1>\r\n"):
    return dict(
        author=author, request_id=key, title=" A Document ", html=html, supersedes_id=supersedes_id
    )


@pytest.mark.parametrize(
    "runtime", ("claude", "codex", "opencode", "gemini", "deepcode", "aider", "shell")
)
async def test_documents_contract(api_app, api_client, auth_headers, runtime):
    await api_app.state.db.agents.update_fields("ike", {"runtime": runtime})
    await author(api_app.state.db, "other")

    async def publish(payload, status=201):
        result = await api_client.post("/api/documents", headers=auth_headers, json=payload)
        assert result.status_code == status, result.text
        return result.json()["record"]

    original = await publish(body())
    other = await publish(body(key="other"))
    correction_body = body(key="correction", supersedes_id=original["id"], html="corrected")
    correction = await publish(correction_body)
    third = await publish(body(key="third", supersedes_id=correction["id"], html="third"))
    retry = await publish(correction_body, 200)
    assert retry["id"] == correction["id"] and retry["superseded_by"] == third["id"]
    assert original["document_id"] == correction["document_id"] == third["document_id"]
    assert original["document_id"] != other["document_id"]
    assert "html" not in original
    for bad in (
        body(html="changed"),
        body(key="stale", supersedes_id=original["id"]),
        body(author="other", supersedes_id=third["id"]),
    ):
        result = await api_client.post("/api/documents", headers=auth_headers, json=bad)
        assert result.status_code == 409
    page = (await api_client.get("/api/documents?limit=1&agent=ike", headers=auth_headers)).json()
    assert [r["id"] for r in page["items"]] == [third["id"]]
    assert page["has_more"] and page["next_before_id"] == third["id"]
    page = (
        await api_client.get(f"/api/documents?before_id={third['id']}", headers=auth_headers)
    ).json()
    assert [r["id"] for r in page["items"]] == [other["id"]]
    assert not page["has_more"] and page["next_before_id"] is None
    assert "html" not in page["items"][0]
    read = (await api_client.get(f"/api/documents/{original['id']}", headers=auth_headers)).json()
    assert read["html"] == body()["html"] and read["title"] == body()["title"]
    assert read["superseded_by"] == correction["id"]
    assert read["content_bytes"] == len(body()["html"].encode())
    assert read["content_sha256"] == hashlib.sha256(body()["html"].encode()).hexdigest()


async def test_auth_and_bounds(api_client, auth_headers):
    auth_headers = auth_headers | {"Content-Type": "application/json"}
    for path in ("/api/documents", "/api/documents/1"):
        assert (await api_client.get(path)).status_code == 401
    assert (await api_client.post("/api/documents", json=body())).status_code == 401
    assert (await api_client.get("/api/documents/1", headers=auth_headers)).status_code == 404
    # Worst-case escaping: each one-byte decoded NUL expands to six wire bytes.
    payload = body(html="\x00" * DOCUMENT_HTML_BYTES)
    raw = json.dumps(payload).encode()
    assert len(raw) < DOCUMENT_WIRE_BYTES and len(raw) > 6 * DOCUMENT_HTML_BYTES
    saved = await api_client.post("/api/documents", headers=auth_headers, content=raw)
    assert saved.status_code == 201, saved.text
    read = await api_client.get(
        f"/api/documents/{saved.json()['record']['id']}", headers=auth_headers
    )
    assert read.json()["html"] == payload["html"] and len(read.content) <= DOCUMENT_WIRE_BYTES
    invalids = [
        body(key="too-big", html="a" * (DOCUMENT_HTML_BYTES + 1)),
        body(html="\ud800"),
        body() | {"title": "\x1b[2J"},
        body() | {"extra": "SECRET"},
        body() | {"\ud800": "SECRET"},
        body() | {"supersedes_id": True},
    ]
    for invalid in invalids:
        response = await api_client.post(
            "/api/documents", headers=auth_headers, content=json.dumps(invalid).encode()
        )
        assert response.status_code == 422
        assert "SECRET" not in response.text and len(response.content) < 4096

    async def chunks():
        yield b" " * DOCUMENT_WIRE_BYTES
        yield b"x"

    assert (
        await api_client.post("/api/documents", headers=auth_headers, content=chunks())
    ).status_code == 413
    for query in ("limit=101", "before_id=0", "author_id=bad"):
        assert (
            await api_client.get("/api/documents?" + query, headers=auth_headers)
        ).status_code == 422


async def test_signed_large_original_bytes_and_other_routes_stay_bounded(
    api_app, api_client, auth_headers, monkeypatch
):
    monkeypatch.setattr("agent_backbone.api.signed.now", lambda: NOW)
    monkeypatch.setattr("agent_backbone.api.routes.signing.now", lambda: NOW)
    db = api_app.state.db
    await author(db, SENDER)
    key = Ed25519PrivateKey.generate()
    await _enroll(api_client, auth_headers, db, key)
    payload = body(author=SENDER, html="é" * 600_000)
    unsigned = await api_client.post("/api/documents", headers=auth_headers, json=payload)
    assert unsigned.status_code == 403
    raw, signed = await _signed(db, key, "POST", "/api/documents", payload)
    changed = await api_client.post(
        "/api/documents", headers=auth_headers | signed, content=raw + b" "
    )
    assert changed.status_code == 403 and changed.json()["detail"]["reason"] == "signature_invalid"
    result = await api_client.post("/api/documents", headers=auth_headers | signed, content=raw)
    assert result.status_code == 201, result.text
    raw, signed = await _signed(db, key, "POST", "/api/documents", payload)
    assert (
        await api_client.post("/api/documents", headers=auth_headers | signed, content=raw)
    ).status_code == 200
    assert (
        await api_client.post("/api/messages", headers=auth_headers, content=b" " * 1_048_577)
    ).status_code == 413
