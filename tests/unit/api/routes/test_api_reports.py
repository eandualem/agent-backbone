"""The authoring API rejects excessive content before storage and never echoes it."""

import json

import pytest

from agent_backbone.models import REPORT_BODY_BYTES, REPORTS_PER_HOUR
from tests.report_support import publication, publish


async def test_publish_read_filter_history_and_schema(api_app, api_client, auth_headers):
    body = publication("ike").model_dump()
    first = await api_client.post("/api/reports", headers=auth_headers, json=body)
    assert first.status_code == 201
    record = first.json()["record"]
    again = await api_client.post("/api/reports", headers=auth_headers, json=body)
    assert again.status_code == 200 and not again.json()["created"]
    assert again.json()["record"]["id"] == record["id"]
    detail = await api_client.get(f"/api/reports/{record['id']}", headers=auth_headers)
    assert detail.json()["report"] == body["report"]
    selected = await api_client.get("/api/reports?agent=ike&agent=ada", headers=auth_headers)
    assert selected.status_code == 200
    assert [entry["agent_name"] for entry in selected.json()["items"]] == ["ike", "ada"]
    assert selected.json()["items"][1]["record"] is None
    history = await api_client.get("/api/reports?history=true&agent=ike", headers=auth_headers)
    assert len(history.json()["items"]) == 1
    schema = await api_client.get("/api/reports/schema", headers=auth_headers)
    assert schema.status_code == 200
    assert {"goal", "progress", "blockers", "next"} <= set(
        schema.json()["report_schema"]["required"]
    )
    assert schema.json()["limits"]["request_bytes"] == REPORT_BODY_BYTES


@pytest.mark.parametrize("path", ["/api/reports", "/api/reports/schema", "/api/reports/1"])
async def test_report_reads_require_authentication(api_client, path):
    assert (await api_client.get(path)).status_code == 401


async def test_report_writes_require_authentication_before_body_read(api_client):
    response = await api_client.post("/api/reports", content=b"x" * (REPORT_BODY_BYTES + 1))
    assert response.status_code == 401


@pytest.mark.parametrize("kind", ["length", "extra", "missing", "control", "url", "total"])
async def test_field_errors_are_short_and_save_nothing(api_app, api_client, auth_headers, kind):
    body = publication("ike").model_dump()
    secret_marker = "REJECTED_PRIVATE_CONTENT"
    if kind == "length":
        body["report"]["progress"]["text"] = secret_marker * 40
    elif kind == "extra":
        body["report"]["log"] = secret_marker
    elif kind == "missing":
        del body["report"]["goal"]
    elif kind == "control":
        body["report"]["goal"]["text"] = "\x1b]8;;https://evil.example\x07click"
    elif kind == "url":
        body["report"]["goal"]["links"][0]["url"] = "javascript:alert(1)"
    else:
        for field, length in (("goal", 240), ("progress", 480), ("blockers", 320), ("next", 320)):
            body["report"][field]["text"] = "a" * length
        body["report"]["note"] = {"text": "a" * 240, "links": []}
    response = await api_client.post("/api/reports", headers=auth_headers, json=body)
    assert response.status_code == 422
    assert secret_marker not in response.text
    assert len(response.content) < 1500
    assert "field" in response.json()["detail"][0]
    page = await api_client.get("/api/reports?history=true", headers=auth_headers)
    assert page.json()["items"] == []


async def test_body_size_is_enforced_for_chunked_requests(api_client, auth_headers):
    async def chunks():
        for _ in range(20):
            yield b" " * 1024

    response = await api_client.post("/api/reports", headers=auth_headers, content=chunks())
    assert response.status_code == 413
    assert str(REPORT_BODY_BYTES) in response.text


async def test_body_size_and_malformed_json_errors_are_bounded(api_client, auth_headers):
    response = await api_client.post(
        "/api/reports", headers=auth_headers, content=b"x" * (REPORT_BODY_BYTES + 1)
    )
    assert response.status_code == 413
    response = await api_client.post(
        "/api/reports",
        headers=auth_headers | {"Content-Type": "application/json"},
        content=b'{"broken":',
    )
    assert response.status_code == 422 and len(response.content) < 500


async def test_conflicting_key_unknown_agent_and_rate_limit(api_app, api_client, auth_headers):
    body = publication("ike").model_dump()
    await api_client.post("/api/reports", headers=auth_headers, json=body)
    body["report"]["goal"]["text"] = "Something else."
    assert (
        await api_client.post("/api/reports", headers=auth_headers, json=body)
    ).status_code == 409
    body["agent"] = "unknown-agent"
    assert (
        await api_client.post("/api/reports", headers=auth_headers, json=body)
    ).status_code == 404
    for n in range(REPORTS_PER_HOUR - 1):
        await publish(api_app.state.db, "ike", str(n))
    response = await api_client.post(
        "/api/reports", headers=auth_headers, json=publication("ike", "too-many").model_dump()
    )
    assert response.status_code == 429 and "Retry-After" in response.headers


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=21",
        "cursor=invalid",
        "agent=unknown",
        "author_id=bad",
        "author_id=" + "a" * 32,
        "agent=bad%0Aname",
    ],
)
async def test_invalid_queries_are_clear(api_client, auth_headers, query):
    response = await api_client.get("/api/reports?" + query, headers=auth_headers)
    assert response.status_code == 422
    assert len(response.content) < 1500


async def test_openapi_exposes_named_reporting_tools(api_app):
    schema = api_app.openapi()
    assert schema["paths"]["/api/reports"]["post"]["operationId"] == "publish_progress_report"
    encoded = json.dumps(schema)
    assert "maxLength" in encoded and "ProgressReport" in encoded


async def test_hostile_extra_field_names_cannot_expand_validation_response(
    api_client, auth_headers
):
    body = publication("ike").model_dump()
    body["report"]["\x1b[2J" + "x" * 10000] = "extra"
    response = await api_client.post("/api/reports", headers=auth_headers, json=body)
    assert response.status_code == 422
    assert len(response.content) < 500
    assert "\x1b" not in response.json()["detail"][0]["field"]
