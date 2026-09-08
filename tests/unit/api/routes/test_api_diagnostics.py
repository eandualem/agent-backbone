"""Diagnostics expose explicit metadata, with bounded filtering and operation scope."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


async def _record(db, operation_id="op-a", **changes):
    values = {
        "operation_id": operation_id,
        "category": "delivery",
        "code": "delivery_failed",
        "severity": "error",
        "agent_name": "ike",
        "runtime": "shell",
        "details": {"stage": "paste", "error_type": "RuntimeError"},
    }
    values.update(changes)
    return await db.diagnostics.record(**values)


async def test_digest_groups_failures_and_preserves_safe_history(api_app, api_client, auth_headers):
    db = api_app.state.db
    await _record(db)
    await _record(db)
    await _record(db, operation_id="op-b", code="expired", severity="warning")
    await db.deliveries.record(
        issue_number=None,
        target_entity="ike",
        session_name="ike",
        outcome="agent_working",
        kind="direct_message",
        preview="PRIVATE_MESSAGE_PREVIEW",
    )
    await db.queue.enqueue(
        session_name="ike", message="PRIVATE_MESSAGE_BODY", delivery_kind="direct_message"
    )
    response = await api_client.get("/api/diagnostics?limit=1", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["total_groups"] == 2
    assert body["total_occurrences"] == 3
    assert body["has_more"] is True
    assert len(body["groups"]) == 1
    assert body["deliveries"]["outcomes"] == {"agent_working": 1}
    assert body["queue"]["pending"] == 1
    assert body["coverage"]["retention_days"] == api_app.state.config.timing.delivery_retention_days
    assert "retained" in body["count_semantics"]
    assert "PRIVATE" not in response.text


async def test_detail_uses_exact_operation_and_omits_unknown_payload_fields(
    api_app, api_client, auth_headers, monkeypatch
):
    db = api_app.state.db
    record_id = await _record(db)
    other_id = await _record(db, operation_id="other-operation")
    raw = await db.diagnostics.get(record_id)
    raw["preview"] = "PRIVATE_PREVIEW"
    raw["details"] = {"stage": "paste", "message": "PRIVATE_BODY", "error_type": "Error with text"}
    monkeypatch.setattr(db.diagnostics, "get", AsyncMock(return_value=raw))
    response = await api_client.get(f"/api/diagnostics/{record_id}", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["record"]["details"] == {"stage": "paste"}
    assert {item["id"] for item in body["operation_records"]} == {record_id}
    assert other_id not in {item["id"] for item in body["operation_records"]}
    assert "PRIVATE" not in response.text
    assert "preview" not in body["record"]


async def test_records_page_and_agent_filter(api_app, api_client, auth_headers):
    db = api_app.state.db
    first_id = await _record(db)
    second_id = await _record(db, operation_id="op-b")
    await _record(db, operation_id="op-c", agent_name="leo")
    response = await api_client.get(
        "/api/diagnostics/records?agent=ike&severity=error&category=delivery&limit=1",
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["items"]] == [second_id]
    assert body["has_more"] is True
    assert body["next_before_id"] == second_id
    response = await api_client.get(
        f"/api/diagnostics/records?agent=ike&limit=1&before_id={second_id}",
        headers=auth_headers,
    )
    body = response.json()
    assert [item["id"] for item in body["items"]] == [first_id]
    assert body["has_more"] is False
    assert body["next_before_id"] is None


async def test_missing_record(api_client, auth_headers):
    response = await api_client.get("/api/diagnostics/987654321", headers=auth_headers)
    assert response.status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/api/diagnostics?limit=0",
        "/api/diagnostics?limit=101",
        "/api/diagnostics?since=2026-09-08T00:00:00",
        "/api/diagnostics?since=24h",
        "/api/diagnostics/records?before_id=0",
        "/api/diagnostics/records?severity=invalid",
        "/api/diagnostics/0",
    ],
)
async def test_invalid_filters_are_rejected(api_client, auth_headers, path):
    assert (await api_client.get(path, headers=auth_headers)).status_code == 422


async def test_diagnostics_requires_auth(api_client, api_key):
    assert (await api_client.get("/api/diagnostics")).status_code == 401
