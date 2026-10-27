"""One retained spreadsheet contract for every runtime; no paid inference."""

from uuid import uuid4

import pytest

from agent_backbone.config import agent_api_key
from tests.sheet_support import edit, set_cell, tracker


@pytest.mark.parametrize(
    "runtime", ("claude", "codex", "opencode", "gemini", "deepcode", "aider", "shell")
)
async def test_sheets_contract(api_app, api_client, auth_headers, runtime, monkeypatch):
    await api_app.state.db.agents.update_fields("ike", {"runtime": runtime})
    body = tracker("ike")
    response = await api_client.post("/api/sheets", headers=auth_headers, json=body.model_dump())
    assert response.status_code == 201, response.text
    first = response.json()["record"]
    root = "/api/sheets/" + first["sheet_id"]
    tab = body.workbook.tabs[0]
    query = {"revision_id": first["revision_id"], "row_limit": 5, "column_limit": 6}
    original = await api_client.get(
        root + f"/tabs/{tab.tab_id}/range", headers=auth_headers, params=query
    )
    assert original.status_code == 200 and original.json()["rows"][1]["row_id"] == tab.row_ids[1]
    change = edit(first["revision_id"], [set_cell(body, True, "boolean", column=2)], author=None)
    agent_headers = {"Authorization": "Bearer " + agent_api_key(api_app.state.config.api_key)}
    assert (
        await api_client.post(root + "/revisions", headers=agent_headers, json=change.model_dump())
    ).status_code == 403
    second = await api_client.post(
        root + "/revisions", headers=auth_headers, json=change.model_dump(exclude_unset=True)
    )
    assert second.status_code == 201, second.text
    retry = await api_client.post(
        root + "/revisions", headers=auth_headers, json=change.model_dump(exclude_unset=True)
    )
    assert (
        retry.status_code == 200
        and retry.json()["record"]["revision_id"] == second.json()["record"]["revision_id"]
    )
    assert (
        await api_client.get(root + f"/tabs/{tab.tab_id}/range", headers=auth_headers, params=query)
    ).json() == original.json()
    stale = await api_client.post(
        root + "/revisions",
        headers=auth_headers,
        json=edit(first["revision_id"], [set_cell(body, "stale")], author=None).model_dump(),
    )
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "stale_revision"

    async def no_paste(*args):
        pass

    monkeypatch.setattr("agent_backbone.api.routes.sheets.deliver_now", no_paste)
    feedback = {
        "author": None,
        "request_id": uuid4().hex,
        "observed_revision_id": first["revision_id"],
        "kind": "question",
        "text": "Explain this entry",
        "anchor": {
            "tab_id": tab.tab_id,
            "row_ids": [tab.row_ids[1]],
            "column_ids": [tab.column_ids[0]],
        },
    }
    result = await api_client.post(root + "/feedback", headers=auth_headers, json=feedback)
    assert result.status_code == 200, result.text
    assert result.json()["delivery"]["queued"] and not result.json()["delivery"]["ok"]
    again = await api_client.post(root + "/feedback", headers=auth_headers, json=feedback)
    assert again.json()["operation_id"] == result.json()["operation_id"]
    listed = await api_client.get("/api/sheets", headers=auth_headers)
    assert listed.json()["items"][0]["sheet_id"] == first["sheet_id"]


async def test_auth_validation_import_export_and_pinned_range(api_client, auth_headers):
    assert (await api_client.get("/api/sheets")).status_code == 401
    bad = tracker("ike").model_dump() | {"unknown": "DO_NOT_ECHO"}
    invalid = await api_client.post("/api/sheets", headers=auth_headers, json=bad)
    assert invalid.status_code == 422 and "DO_NOT_ECHO" not in invalid.text
    payload = {
        "author": "ike",
        "request_id": uuid4().hex,
        "title": "CSV example",
        "format": "csv",
        "content": "a,b\n=1+2,true",
    }
    imported = await api_client.post("/api/sheets/import", headers=auth_headers, json=payload)
    assert imported.status_code == 201, imported.text
    assert imported.json()["warnings"]
    retry = await api_client.post("/api/sheets/import", headers=auth_headers, json=payload)
    assert retry.status_code == 200 and retry.json()["warnings"] == imported.json()["warnings"]
    record = imported.json()["record"]
    root = "/api/sheets/" + record["sheet_id"]
    exported = await api_client.get(
        root + "/export",
        headers=auth_headers,
        params={"revision_id": record["revision_id"], "format": "json"},
    )
    assert (
        exported.status_code == 200
        and exported.json()["tabs"][0]["cells"][2]["input"]["value"] == "=1+2"
    )
    path = root + "/tabs/" + record["tabs"][0]["tab_id"] + "/range"
    assert (await api_client.get(path, headers=auth_headers)).status_code == 422
    assert (
        await api_client.get(
            path,
            headers=auth_headers,
            params={"revision_id": record["revision_id"], "row_limit": 200, "column_limit": 100},
        )
    ).status_code == 422


async def test_signed_author_and_final_range_guard(api_app, api_client, auth_headers, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from tests.report_support import author
    from tests.unit.api.routes.test_api_signing import NOW, SENDER, _enroll, _signed

    monkeypatch.setattr("agent_backbone.api.signed.now", lambda: NOW)
    monkeypatch.setattr("agent_backbone.api.routes.signing.now", lambda: NOW)
    db = api_app.state.db
    await author(db, SENDER)
    key = Ed25519PrivateKey.generate()
    await _enroll(api_client, auth_headers, db, key)
    body = tracker(SENDER)
    payload = body.model_dump()
    unsigned = await api_client.post("/api/sheets", headers=auth_headers, json=payload)
    assert unsigned.status_code == 403
    raw, signed = await _signed(db, key, "POST", "/api/sheets", payload)
    created = await api_client.post("/api/sheets", headers=auth_headers | signed, content=raw)
    assert created.status_code == 201, created.text
    record = created.json()["record"]
    root = "/api/sheets/" + record["sheet_id"]
    change = edit(record["revision_id"], [set_cell(body, "Signed")], author=SENDER).model_dump(
        exclude_unset=True
    )
    raw, signed = await _signed(db, key, "POST", root + "/revisions", change)
    assert (
        await api_client.post(root + "/revisions", headers=auth_headers | signed, content=raw)
    ).status_code == 201
    monkeypatch.setattr("agent_backbone.api.routes.sheets.SHEET_RANGE_BYTES", 1)
    path = root + "/tabs/" + body.workbook.tabs[0].tab_id + "/range"
    refused = await api_client.get(
        path, headers=auth_headers, params={"revision_id": record["revision_id"]}
    )
    assert refused.status_code == 413 and refused.json()["detail"]["code"] == "range_too_large"


async def test_invalid_utf8_import_is_validation_error(api_client, auth_headers):
    import json

    payload = {
        "author": "ike",
        "request_id": uuid4().hex,
        "title": "Example",
        "format": "csv",
        "content": "\ud800",
    }
    result = await api_client.post(
        "/api/sheets/import", headers=auth_headers, content=json.dumps(payload).encode()
    )
    assert result.status_code == 422
