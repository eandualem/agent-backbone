"""Artifact conversations: one contract for every runtime (docs/conversations.md)."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from agent_backbone.config import agent_api_key
from tests.conftest import TEST_API_KEY
from tests.unit.api.routes.test_api_responses import body as response_body

AGENT = {"Authorization": f"Bearer {agent_api_key(TEST_API_KEY)}"}


@pytest.fixture
def deliver(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr("agent_backbone.api.routes.conversations.deliver_now", mock)
    return mock


async def _document(client, headers, author="ike", supersedes_id=None, key="first"):
    result = await client.post(
        "/api/documents",
        headers=headers,
        json={
            "author": author,
            "request_id": key,
            "title": "Benchmark runs",
            "html": f"<p>{key}</p>",
            "supersedes_id": supersedes_id,
        },
    )
    assert result.status_code == 201, result.text
    return result.json()["record"]


async def _queued(db, operation):
    async with db.engine.begin() as conn:
        return (
            (
                await conn.execute(
                    text(
                        "SELECT * FROM message_queue m JOIN conversation_entries e"
                        " ON e.operation_id = m.operation_id WHERE e.id = :id"
                    ),
                    {"id": operation},
                )
            )
            .mappings()
            .one()
        )


@pytest.mark.parametrize("runtime", ("claude", "codex", "opencode", "gemini", "deepcode"))
async def test_ask_reply_and_revise_round_trip(api_app, api_client, auth_headers, deliver, runtime):
    db = api_app.state.db
    await db.agents.update_fields("ike", {"runtime": runtime})
    first = await _document(api_client, auth_headers)
    path = f"/api/conversations/document/{first['document_id']}"

    empty = (await api_client.get(path, headers=auth_headers)).json()
    assert empty["entries"] == [] and empty["total"] == 0
    assert empty["responsible"] == {
        "agent_id": first["author_id"],
        "name": "ike",
        "status": "active",
    }

    ask = {
        "request_id": "ask-1",
        "text": "Could History come before column paging?",
        "anchor": {"revision_id": str(first["id"]), "label": "Runs!B3:D5"},
    }
    sent = await api_client.post(path + "/messages", headers=auth_headers, json=ask)
    assert sent.status_code == 201, sent.text
    entry = sent.json()["entry"]
    assert entry["recipient"]["name"] == "ike" and entry["delivery"] == "pending"
    deliver.assert_awaited_once()
    assert deliver.await_args.args[3] == "ike"
    row = await _queued(db, entry["id"])
    assert row["session_name"] == "ike" and row["delivery_kind"] == "direct_message"
    assert row["message"].startswith(
        f"[via:backbone conversation:document/{first['document_id']} from:owner]"
        ' On "Benchmark runs" (rev 1, Runs!B3:D5):\nCould History'
    )
    assert f"--to {entry['id']}" in row["message"]
    retry = await api_client.post(path + "/messages", headers=auth_headers, json=ask)
    assert retry.status_code == 200 and retry.json()["entry"]["id"] == entry["id"]
    assert deliver.await_count == 1

    assert [r["id"] for r in await db.queue.dequeue("ike")] == [row["id"]]
    await db.queue.mark_delivered(row["id"])
    second = await _document(api_client, auth_headers, supersedes_id=first["id"], key="second")
    reply = {
        "request_id": "reply-1",
        "author": "ike",
        "text": "Done, see rev 2.",
        "in_reply_to": entry["id"],
        "resulting_revision_id": str(second["id"]),
    }
    answered = await api_client.post(path + "/messages", headers=AGENT, json=reply)
    assert answered.status_code == 201, answered.text

    page = (await api_client.get(path, headers=auth_headers)).json()
    kinds = [(e["kind"], e["author"]["type"]) for e in page["entries"]]
    assert kinds == [("message", "owner"), ("revision", "agent"), ("message", "agent")]
    assert page["entries"][0]["delivery"] == "delivered"
    assert page["entries"][1]["revision"] == {"revision_id": str(second["id"]), "label": "rev 2"}
    assert page["entries"][2]["resulting_revision_id"] == str(second["id"])
    assert page["latest_revision_id"] == str(second["id"])
    assert (page["total"], page["unread"], page["read_through"]) == (2, 2, 0)

    read = await api_client.post(
        path + "/read", headers=auth_headers, json={"read_through": page["entries"][-1]["id"]}
    )
    assert read.json() == {"read_through": page["entries"][-1]["id"], "unread": 0, "total": 2}
    back = await api_client.post(path + "/read", headers=auth_headers, json={"read_through": 1})
    assert back.json()["read_through"] == page["entries"][-1]["id"]

    fresh = await api_client.get(path, headers=auth_headers)
    unchanged = await api_client.get(
        path, headers=auth_headers | {"If-None-Match": fresh.headers["ETag"]}
    )
    assert unchanged.status_code == 304

    versions = (
        await api_client.get(
            f"/api/documents/{first['document_id']}/revisions", headers=auth_headers
        )
    ).json()
    assert [v["revision_id"] for v in versions["items"]] == [str(second["id"]), str(first["id"])]
    old = await api_client.get(f"/api/documents/{first['id']}", headers=auth_headers)
    assert old.json()["html"] == "<p>first</p>"

    summaries = (await api_client.get("/api/conversations", headers=auth_headers)).json()
    assert summaries["items"] == [
        {
            "kind": "document",
            "artifact_id": first["document_id"],
            "responsible": page["responsible"],
            "unread": 0,
            "total": 2,
            "last_entry_at": page["entries"][-1]["created_at"],
        }
    ]
    since = summaries["cursor"].split(".")[0]  # entries only: no read-marker overlap
    later = (await api_client.get(f"/api/conversations?since={since}", headers=auth_headers)).json()
    assert later["items"] == []

    assert await db.queue.prune(0) == 1
    kept = (await api_client.get(path, headers=auth_headers)).json()
    assert kept["entries"][0]["delivery"] == "delivered"


async def test_refusals_are_typed(api_app, api_client, auth_headers, deliver):
    db = api_app.state.db
    doc = await _document(api_client, auth_headers)
    other = await _document(api_client, auth_headers, author="leo", key="other")
    path = f"/api/conversations/document/{doc['document_id']}/messages"

    async def refused(payload, status, error, headers=auth_headers, at=path):
        result = await api_client.post(at, headers=headers, json=payload)
        assert result.status_code == status, result.text
        assert result.json()["error"] == error, result.text

    await refused({"request_id": "a", "text": "x" * 4001}, 422, "too_long")
    await refused(
        {"request_id": "a", "text": "hi", "recipient": "nobody"}, 422, "unknown_recipient"
    )
    await refused(
        {"request_id": "a", "text": "hi", "anchor": {"revision_id": str(other["id"])}},
        422,
        "invalid_revision",
    )
    sent = await api_client.post(path, headers=auth_headers, json={"request_id": "a", "text": "hi"})
    assert sent.status_code == 201
    await refused({"request_id": "a", "text": "changed"}, 409, "request_conflict")
    await refused(
        {"request_id": "b", "author": "leo", "text": "not mine"}, 403, "not_participant", AGENT
    )
    await refused(
        {
            "request_id": "c",
            "author": "ike",
            "text": "x",
            "resulting_revision_id": str(other["id"]),
        },
        422,
        "invalid_revision",
        AGENT,
    )
    await refused(
        {"request_id": "c", "author": "ike", "text": "x", "in_reply_to": 999}, 422, "invalid_reply"
    )
    owner_only = await api_client.post(path, headers=AGENT, json={"request_id": "d", "text": "x"})
    assert owner_only.status_code == 403

    api_app.state.config.settings["conversations.owner_messages_per_hour"] = 1
    await refused({"request_id": "e", "text": "again"}, 429, "rate_limited")
    api_app.state.config.settings["conversations.max_messages"] = 1
    await refused({"request_id": "e", "text": "again"}, 409, "conversation_full")

    # A retired author: the conversation stays readable and asks for a recipient.
    await db.agents.delete("ike")
    header = (
        await api_client.get(
            f"/api/conversations/document/{doc['document_id']}", headers=auth_headers
        )
    ).json()
    assert header["responsible"]["status"] == "retired"
    api_app.state.config.settings["conversations.max_messages"] = 100
    api_app.state.config.settings["conversations.owner_messages_per_hour"] = 100
    await refused({"request_id": "f", "text": "who now?"}, 422, "recipient_required")
    to_leo = await api_client.post(
        path, headers=auth_headers, json={"request_id": "f", "text": "who?", "recipient": "leo"}
    )
    assert to_leo.json()["entry"]["recipient"]["name"] == "leo"

    for missing in ("/api/conversations/document/" + "0" * 32, "/api/conversations/sheet/x"):
        result = await api_client.get(missing, headers=auth_headers)
        assert result.status_code == 404 and result.json()["error"] == "unknown_artifact"


async def test_redeliver_failed_message_without_a_new_entry(
    api_app, api_client, auth_headers, deliver
):
    db = api_app.state.db
    doc = await _document(api_client, auth_headers)
    base = f"/api/conversations/document/{doc['document_id']}"
    entry = (
        await api_client.post(
            base + "/messages", headers=auth_headers, json={"request_id": "a", "text": "hi"}
        )
    ).json()["entry"]
    again = base + f"/messages/{entry['id']}/redeliver"
    pending = await api_client.post(again, headers=auth_headers, json={"request_id": "r0"})
    assert pending.json()["result"] == "already_pending"
    async with db.engine.begin() as conn:
        await conn.execute(text("UPDATE message_queue SET status = 'expired'"))
    refreshed = await api_client.get(
        base + f"?entries={entry['id']},999,{entry['id'] + 1000}", headers=auth_headers
    )
    assert [e["delivery"] for e in refreshed.json()["entries"]] == ["failed"]
    assert refreshed.json()["older"] is refreshed.json()["newer"] is None
    first = await api_client.post(again, headers=auth_headers, json={"request_id": "r1"})
    assert first.json()["result"] == "queued" and first.json()["entry"]["delivery"] == "pending"
    repeat = await api_client.post(again, headers=auth_headers, json={"request_id": "r1"})
    assert repeat.json()["result"] == "queued"
    async with db.engine.begin() as conn:
        rows = await conn.scalar(text("SELECT count(*) FROM message_queue"))
        entries = await conn.scalar(text("SELECT count(*) FROM conversation_entries"))
    assert (rows, entries) == (2, 1)
    assert deliver.await_count == 2


async def test_agent_reads_only_its_conversations(api_app, api_client, auth_headers, deliver):
    doc = await _document(api_client, auth_headers)
    path = f"/api/conversations/document/{doc['document_id']}"
    await api_client.post(
        path + "/messages", headers=auth_headers, json={"request_id": "a", "text": "hi"}
    )
    assert (await api_client.get(path + "?agent=ike", headers=AGENT)).status_code == 200
    denied = await api_client.get(path + "?agent=leo", headers=AGENT)
    assert denied.status_code == 403 and denied.json()["error"] == "not_participant"
    assert (await api_client.get(path, headers=AGENT)).status_code == 403
    mine = (await api_client.get("/api/conversations?agent=ike", headers=AGENT)).json()
    theirs = (await api_client.get("/api/conversations?agent=leo", headers=AGENT)).json()
    assert len(mine["items"]) == 1 and theirs["items"] == []


async def test_response_chain_is_one_conversation(api_app, api_client, auth_headers, deliver):
    first = await api_client.post(
        "/api/responses", headers=auth_headers, json=response_body(agent="ike")
    )
    first = first.json()["record"]
    correction = await api_client.post(
        "/api/responses",
        headers=auth_headers,
        json=response_body(agent="ike", key="fix", supersedes_id=first["id"]),
    )
    correction = correction.json()["record"]
    assert first["conversation_id"] == correction["conversation_id"] == first["id"]
    listed = (await api_client.get("/api/responses", headers=auth_headers)).json()
    assert {r["conversation_id"] for r in listed["items"]} == {first["id"]}
    page = (
        await api_client.get(f"/api/conversations/response/{first['id']}", headers=auth_headers)
    ).json()
    assert [e["revision"]["revision_id"] for e in page["entries"]] == [str(correction["id"])]
    versions = (
        await api_client.get(f"/api/responses/{first['id']}/revisions", headers=auth_headers)
    ).json()
    assert [v["revision_id"] for v in versions["items"]] == [
        str(correction["id"]),
        str(first["id"]),
    ]
    not_root = await api_client.get(
        f"/api/conversations/response/{correction['id']}", headers=auth_headers
    )
    assert not_root.status_code == 404 and str(first["id"]) in not_root.json()["message"]


async def test_responsible_agent_lists_conversations_addressed_to_others(
    api_client, auth_headers, deliver
):
    doc = await _document(api_client, auth_headers)
    await api_client.post(
        f"/api/conversations/document/{doc['document_id']}/messages",
        headers=auth_headers,
        json={"request_id": "a", "text": "Leo, can you check this?", "recipient": "leo"},
    )
    for agent in ("ike", "leo"):
        listed = (await api_client.get(f"/api/conversations?agent={agent}", headers=AGENT)).json()
        assert [item["artifact_id"] for item in listed["items"]] == [doc["document_id"]]


async def test_summary_polling_reports_read_marker_changes(api_client, auth_headers, deliver):
    doc = await _document(api_client, auth_headers)
    await _document(api_client, auth_headers, supersedes_id=doc["id"], key="second")
    first = (await api_client.get("/api/conversations", headers=auth_headers)).json()
    assert first["items"][0]["unread"] == 1
    path = f"/api/conversations/document/{doc['document_id']}"
    page = (await api_client.get(path, headers=auth_headers)).json()
    await api_client.post(
        path + "/read", headers=auth_headers, json={"read_through": page["entries"][-1]["id"]}
    )
    polled = (
        await api_client.get(f"/api/conversations?since={first['cursor']}", headers=auth_headers)
    ).json()
    assert [item["unread"] for item in polled["items"]] == [0]


async def test_concurrent_redeliveries_queue_one_delivery(
    api_app, api_client, auth_headers, deliver
):
    import asyncio

    doc = await _document(api_client, auth_headers)
    base = f"/api/conversations/document/{doc['document_id']}"
    entry = (
        await api_client.post(
            base + "/messages", headers=auth_headers, json={"request_id": "a", "text": "hi"}
        )
    ).json()["entry"]
    async with api_app.state.db.engine.begin() as conn:
        await conn.execute(text("UPDATE message_queue SET status = 'expired'"))
    again = base + f"/messages/{entry['id']}/redeliver"
    results = await asyncio.gather(
        *(
            api_client.post(again, headers=auth_headers, json={"request_id": f"r{i}"})
            for i in (1, 2)
        )
    )
    assert sorted(r.json()["result"] for r in results) == ["already_pending", "queued"]
