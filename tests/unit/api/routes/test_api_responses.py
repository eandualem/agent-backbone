"""Stored answers share delivery machinery, never the progress feed."""

import pytest

from agent_backbone.models import RESPONSE_BODY_BYTES
from tests.report_support import publication


def body(agent="ike", key="first", supersedes_id=None):
    return {
        "agent": agent,
        "request_id": key,
        "response": {
            "question": "Can I use the updated command?",
            "answer": f"Yes. The checks passed. {key}",
            "action": "Try it when ready.\nNo decision is needed.",
        },
        "supersedes_id": supersedes_id,
    }


@pytest.mark.parametrize(
    "runtime", ("claude", "codex", "opencode", "gemini", "deepcode", "aider", "shell")
)
async def test_response_publication_retrieval_and_correction(
    api_app, api_client, auth_headers, runtime
):
    await api_app.state.db.agents.update_fields("ike", {"runtime": runtime})
    first = await api_client.post("/api/responses", headers=auth_headers, json=body())
    assert first.status_code == 201
    original = first.json()["record"]
    retry = await api_client.post("/api/responses", headers=auth_headers, json=body())
    assert retry.status_code == 200 and not retry.json()["created"]
    assert retry.json()["record"]["id"] == original["id"]
    second = await api_client.post(
        "/api/responses",
        headers=auth_headers,
        json=body(key="corrected", supersedes_id=original["id"]),
    )
    assert second.status_code == 201
    correction = second.json()["record"]
    read = await api_client.get(f"/api/responses/{original['id']}", headers=auth_headers)
    assert read.json()["superseded_by"] == correction["id"]
    assert read.json()["response"] == body()["response"]
    page = await api_client.get("/api/responses?agent=ike&limit=1", headers=auth_headers)
    assert [item["id"] for item in page.json()["items"]] == [correction["id"]]
    assert page.json()["has_more"]
    older = await api_client.get(
        f"/api/responses?before_id={page.json()['next_before_id']}", headers=auth_headers
    )
    assert older.json()["items"][0]["superseded_by"] == correction["id"]

    # The same key may publish a report without colliding or mixing either feed.
    report = await api_client.post(
        "/api/reports", headers=auth_headers, json=publication("ike").model_dump()
    )
    report_id = report.json()["record"]["id"]
    assert (
        await api_client.get(f"/api/reports/{original['id']}", headers=auth_headers)
    ).status_code == 404
    assert (
        await api_client.get(f"/api/responses/{report_id}", headers=auth_headers)
    ).status_code == 404
    history = await api_client.get("/api/reports?history=true", headers=auth_headers)
    assert [item["record"]["id"] for item in history.json()["items"]] == [report_id]


async def test_response_rejects_changed_retry_or_other_author_correction(api_client, auth_headers):
    first = await api_client.post("/api/responses", headers=auth_headers, json=body())
    changed = body()
    changed["response"]["answer"] = "Changed content."
    assert (
        await api_client.post("/api/responses", headers=auth_headers, json=changed)
    ).status_code == 409
    foreign = body(agent="ada", supersedes_id=first.json()["record"]["id"])
    assert (
        await api_client.post("/api/responses", headers=auth_headers, json=foreign)
    ).status_code == 409


async def test_response_auth_and_bounded_validation(api_client, auth_headers):
    for path in ("/api/responses", "/api/responses/1", "/api/responses/schema"):
        assert (await api_client.get(path)).status_code == 401
    raw = b"x" * (RESPONSE_BODY_BYTES + 1)
    assert (await api_client.post("/api/responses", content=raw)).status_code == 401
    assert (
        await api_client.post("/api/responses", headers=auth_headers, content=raw)
    ).status_code == 413
    invalid = body()
    invalid["response"]["answer"] = "REJECTED_PRIVATE_CONTENT" * 500
    response = await api_client.post("/api/responses", headers=auth_headers, json=invalid)
    assert response.status_code == 422
    assert "REJECTED_PRIVATE_CONTENT" not in response.text
    invalid["response"]["answer"] = "[2J"
    assert (
        await api_client.post("/api/responses", headers=auth_headers, json=invalid)
    ).status_code == 422
