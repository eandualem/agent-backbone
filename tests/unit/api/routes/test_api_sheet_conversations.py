"""Sheet discussions pin immutable revisions without granting edit authority."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import text

from tests.report_support import author
from tests.sheet_support import edit, set_cell, tracker
from tests.unit.api.routes.test_api_conversations import AGENT, _queued


@pytest.fixture
def deliver(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr("agent_backbone.api.routes.conversations.deliver_now", mock)
    return mock


async def create(client, headers):
    body = tracker("ike")
    result = await client.post("/api/sheets", headers=headers, json=body.model_dump())
    assert result.status_code == 201, result.text
    return body, result.json()["record"]


async def change(client, headers, sheet, request):
    result = await client.post(
        f"/api/sheets/{sheet['sheet_id']}/revisions",
        headers=headers,
        json=request.model_dump(exclude_unset=True),
    )
    assert result.status_code == 201, result.text
    return result.json()["record"]


def selection(body, revision):
    tab = body.workbook.tabs[0]
    return {
        "revision_id": revision,
        "sheet": {
            "tab_id": tab.tab_id,
            "row_ids": [tab.row_ids[1]],
            "column_ids": [tab.column_ids[0]],
        },
        "label": "Opportunities!A2",
    }


@pytest.mark.parametrize(
    "runtime", ("claude", "codex", "opencode", "gemini", "deepcode", "aider", "shell")
)
async def test_sheet_question_revision_reply_contract(
    api_app, api_client, auth_headers, deliver, runtime
):
    db = api_app.state.db
    await db.agents.update_fields("ike", {"runtime": runtime})
    body, first = await create(api_client, auth_headers)
    path = f"/api/conversations/sheet/{first['sheet_id']}"
    ask = {
        "request_id": "question",
        "text": "Please update this entry",
        "anchor": selection(body, first["revision_id"]),
    }
    response = await api_client.post(path + "/messages", headers=auth_headers, json=ask)
    assert response.status_code == 201, response.text
    entry = response.json()["entry"]
    assert entry["anchor"] == ask["anchor"]
    queue = await _queued(db, entry["id"])
    assert queue["registration_id"] == first["author_id"]
    assert first["revision_id"] in queue["message"]
    assert body.workbook.tabs[0].row_ids[1] in queue["message"]
    assert "backbone conversations reply sheet" in queue["message"]
    await db.queue.mark_delivered(queue["id"])
    request = edit(first["revision_id"], [set_cell(body, "Updated")], author="ike")
    second = await change(api_client, auth_headers, first, request)
    reply = await api_client.post(
        path + "/messages",
        headers=AGENT,
        json={
            "author": "ike",
            "request_id": "answer",
            "text": "Updated the entry.",
            "in_reply_to": entry["id"],
            "resulting_revision_id": second["revision_id"],
        },
    )
    assert reply.status_code == 201, reply.text
    page = (await api_client.get(path, headers=auth_headers)).json()
    assert [e["kind"] for e in page["entries"]] == ["revision", "message", "revision", "message"]
    assert page["latest_revision_id"] == second["revision_id"]
    assert page["entries"][-1]["in_reply_to"] == entry["id"]
    again = await api_client.post(path + "/messages", headers=auth_headers, json=ask)
    assert again.status_code == 200 and again.json()["entry"]["id"] == entry["id"]
    assert deliver.await_count == 1
    retry = await api_client.post(
        f"/api/sheets/{first['sheet_id']}/revisions",
        headers=auth_headers,
        json=request.model_dump(exclude_unset=True),
    )
    assert retry.status_code == 200
    assert (await api_client.get(path, headers=auth_headers)).json() == page
    # Exact old content is still readable and never recalculated by the conversation.
    old = await api_client.get(
        f"/api/sheets/{first['sheet_id']}",
        headers=auth_headers,
        params={"revision_id": first["revision_id"]},
    )
    assert old.status_code == 200 and old.json()["revision_id"] == first["revision_id"]
    history = (
        await api_client.get(f"/api/sheets/{first['sheet_id']}/revisions", headers=auth_headers)
    ).json()
    assert [r["revision_id"] for r in history["items"]] == [
        second["revision_id"],
        first["revision_id"],
    ]
    mine = (await api_client.get("/api/conversations?kind=sheet&agent=ike", headers=AGENT)).json()
    assert mine["items"][0]["artifact_id"] == first["sheet_id"]
    assert (await api_client.get(path + "?agent=brunel", headers=AGENT)).status_code == 403


async def test_old_sheet_anchor_survives_deleted_selection_and_redelivery(
    api_app, api_client, auth_headers, deliver
):
    body, first = await create(api_client, auth_headers)
    path = f"/api/conversations/sheet/{first['sheet_id']}"
    tab = body.workbook.tabs[0]
    # Remove the selected tab; a historical selection still refers to the old workbook.
    second = await change(
        api_client,
        auth_headers,
        first,
        edit(first["revision_id"], [{"op": "delete_tab", "tab_id": tab.tab_id}], author=None),
    )
    ask = {
        "request_id": "old-selection",
        "text": "Explain this old row",
        "anchor": selection(body, first["revision_id"]),
    }
    result = await api_client.post(path + "/messages", headers=auth_headers, json=ask)
    assert result.status_code == 201, result.text
    entry = result.json()["entry"]
    assert entry["anchor"]["revision_id"] == first["revision_id"]
    async with api_app.state.db.engine.begin() as conn:
        await conn.execute(
            text("UPDATE message_queue SET status='expired' WHERE source='conversation'")
        )
    resent = await api_client.post(
        path + f"/messages/{entry['id']}/redeliver",
        headers=auth_headers,
        json={"request_id": "resend"},
    )
    assert resent.json()["result"] == "queued"
    queued = await _queued(api_app.state.db, entry["id"])
    assert f"rev {first['revision_id']}" in queued["message"]
    assert second["revision_id"] not in queued["message"]
    invalid = await api_client.post(
        path + "/messages",
        headers=auth_headers,
        json=ask | {"request_id": "deleted-now", "anchor": selection(body, second["revision_id"])},
    )
    assert invalid.status_code == 422 and invalid.json()["error"] == "invalid_anchor"


async def test_sheet_owner_is_stable_and_reply_cannot_claim_operator_or_foreign_revision(
    api_app, api_client, auth_headers, deliver
):
    db = api_app.state.db
    body, first = await create(api_client, auth_headers)
    second = await change(
        api_client,
        auth_headers,
        first,
        edit(first["revision_id"], [set_cell(body, "Operator edit")], author=None),
    )
    path = f"/api/conversations/sheet/{first['sheet_id']}"
    page = (await api_client.get(path, headers=auth_headers)).json()
    assert page["responsible"]["agent_id"] == first["author_id"]
    assert page["entries"][-1]["author"] == {"type": "owner", "agent_id": None, "name": "operator"}
    # Even an operator-only event is included in the registered owner's summary.
    async with db.engine.begin() as conn:
        await conn.execute(text("DELETE FROM conversation_entries WHERE author_type='agent'"))
    assert (await api_client.get("/api/conversations?kind=sheet&agent=ike", headers=AGENT)).json()[
        "items"
    ]
    for revision in (second["revision_id"], uuid4().hex):
        result = await api_client.post(
            path + "/messages",
            headers=AGENT,
            json={
                "author": "ike",
                "request_id": uuid4().hex,
                "text": "I revised it",
                "resulting_revision_id": revision,
            },
        )
        assert result.status_code == 422 and result.json()["error"] == "invalid_revision"
    own_older = await api_client.post(
        path + "/messages",
        headers=AGENT,
        json={
            "author": "ike",
            "request_id": "earlier-work",
            "text": "I published the original.",
            "resulting_revision_id": first["revision_id"],
        },
    )
    assert own_older.status_code == 201
    addressed = await api_client.post(
        path + "/messages",
        headers=auth_headers,
        json={"request_id": "other", "text": "Please explain", "recipient": "brunel"},
    )
    assert addressed.status_code == 201
    assert (await api_client.get(path + "?agent=brunel", headers=AGENT)).status_code == 200
    forbidden = await api_client.post(
        f"/api/sheets/{first['sheet_id']}/revisions",
        headers=AGENT,
        json=edit(
            second["revision_id"], [set_cell(body, "Not authorized")], author="brunel"
        ).model_dump(),
    )
    assert forbidden.status_code == 403
    await db.agents.rename("ike", "renamed")
    assert (await api_client.get(path, headers=auth_headers)).json()["responsible"][
        "name"
    ] == "renamed"
    await db.agents.delete("renamed")
    await author(db, "ike")
    retired = (await api_client.get(path, headers=auth_headers)).json()
    assert retired["responsible"]["status"] == "retired"
    assert (await api_client.get(path + "?agent=ike", headers=AGENT)).status_code == 403
    result = await api_client.post(
        path + "/messages",
        headers=auth_headers,
        json={"request_id": "retired", "text": "Who owns this?"},
    )
    assert result.status_code == 422 and result.json()["error"] == "recipient_required"


@pytest.mark.parametrize(
    "bad", ("missing_revision", "foreign_revision", "missing_row", "duplicate", "too_many", "item")
)
async def test_sheet_selection_refused_before_queue(
    api_app, api_client, auth_headers, deliver, bad
):
    body, first = await create(api_client, auth_headers)
    anchor = selection(body, first["revision_id"])
    if bad == "missing_revision":
        anchor.pop("revision_id")
    if bad == "foreign_revision":
        other = tracker("ike").model_dump()
        other["request_id"] = uuid4().hex
        created = await api_client.post("/api/sheets", headers=auth_headers, json=other)
        assert created.status_code == 201
        anchor["revision_id"] = created.json()["record"]["revision_id"]
    if bad == "missing_row":
        anchor["sheet"]["row_ids"] = [uuid4().hex]
    if bad == "duplicate":
        anchor["sheet"]["row_ids"] *= 2
    if bad == "too_many":
        anchor["sheet"]["row_ids"] = [uuid4().hex for _ in range(11)]
        anchor["sheet"]["column_ids"] = [uuid4().hex for _ in range(10)]
    if bad == "item":
        anchor["item_id"] = "a-plan-item"
    result = await api_client.post(
        f"/api/conversations/sheet/{first['sheet_id']}/messages",
        headers=auth_headers,
        json={"request_id": "invalid", "text": "Explain", "anchor": anchor},
    )
    assert result.status_code == 422
    assert result.json()["error"] == (
        "invalid_revision" if bad == "foreign_revision" else "invalid_anchor"
    )
    deliver.assert_not_awaited()
    async with api_app.state.db.engine.begin() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM message_queue")) == 0
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM conversation_entries WHERE entry_kind='message'")
            )
            == 0
        )


async def test_sheet_pre_upgrade_baseline_and_publication_rollback(
    api_app, api_client, auth_headers, deliver, monkeypatch
):
    import agent_backbone.services.database._sheets_repo as module

    db = api_app.state.db
    body, first = await create(api_client, auth_headers)
    path = f"/api/conversations/sheet/{first['sheet_id']}"
    async with db.engine.begin() as conn:
        await conn.execute(text("DELETE FROM conversation_entries"))
    assert (await api_client.get(path, headers=auth_headers)).json()["entries"] == []
    original = module.record_revision

    async def fail_after_event(*args):
        await original(*args)
        raise RuntimeError("revision event failed")

    request = edit(first["revision_id"], [set_cell(body, "Changed")], author="ike")
    with monkeypatch.context() as patch:
        patch.setattr(module, "record_revision", fail_after_event)
        with pytest.raises(RuntimeError, match="revision event failed"):
            await change(api_client, auth_headers, first, request)
    current = (
        await api_client.get(f"/api/sheets/{first['sheet_id']}", headers=auth_headers)
    ).json()
    assert current["revision_id"] == first["revision_id"]
    assert (await api_client.get(path, headers=auth_headers)).json()["entries"] == []
    second = await change(api_client, auth_headers, first, request)
    page = (await api_client.get(path, headers=auth_headers)).json()
    assert [e["revision"]["revision_id"] for e in page["entries"]] == [
        first["revision_id"],
        second["revision_id"],
    ]


async def test_first_message_records_only_current_pre_adapter_baseline(
    api_app, api_client, auth_headers, deliver
):
    body, first = await create(api_client, auth_headers)
    second = await change(
        api_client,
        auth_headers,
        first,
        edit(first["revision_id"], [set_cell(body, "Updated")], author="ike"),
    )
    path = f"/api/conversations/sheet/{first['sheet_id']}"
    async with api_app.state.db.engine.begin() as conn:
        await conn.execute(text("DELETE FROM conversation_entries"))
    # History predates adapter activation: do not fabricate events for the older edit.
    ask = {
        "request_id": "pre-adapter",
        "text": "Explain the original",
        "anchor": selection(body, first["revision_id"]),
    }
    sent = await api_client.post(path + "/messages", headers=auth_headers, json=ask)
    assert sent.status_code == 201
    page = (await api_client.get(path, headers=auth_headers)).json()
    assert [e["kind"] for e in page["entries"]] == ["revision", "message"]
    assert page["entries"][0]["revision"]["revision_id"] == second["revision_id"]
    assert page["entries"][1]["anchor"]["revision_id"] == first["revision_id"]


async def test_operator_revisions_do_not_use_owner_message_allowance(
    api_app, api_client, auth_headers, deliver
):
    api_app.state.config.settings["conversations.owner_messages_per_hour"] = 1
    body, first = await create(api_client, auth_headers)
    second = await change(
        api_client,
        auth_headers,
        first,
        edit(first["revision_id"], [set_cell(body, "Operator update")], author=None),
    )
    path = f"/api/conversations/sheet/{first['sheet_id']}"
    ask = {"request_id": "first-question", "text": "Explain the update"}
    first_question = await api_client.post(path + "/messages", headers=auth_headers, json=ask)
    assert first_question.status_code == 201, first_question.text
    retry = await api_client.post(path + "/messages", headers=auth_headers, json=ask)
    assert retry.status_code == 200
    refused = await api_client.post(
        path + "/messages", headers=auth_headers, json=ask | {"request_id": "second-question"}
    )
    assert refused.status_code == 429 and refused.json()["error"] == "rate_limited"
    deliver.assert_awaited_once()
    # Publishing stays available even when the owner's messaging allowance is exhausted.
    await change(
        api_client,
        auth_headers,
        second,
        edit(second["revision_id"], [set_cell(body, "Another update")], author=None),
    )
    page = (await api_client.get(path, headers=auth_headers)).json()
    assert [e["kind"] for e in page["entries"]] == ["revision", "revision", "message", "revision"]
    assert page["entries"][-1]["author"]["type"] == "owner"
