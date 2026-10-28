"""Current-only plans in the shared conversation contract, for every API caller."""

from copy import deepcopy
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import text

from tests.report_support import author
from tests.unit.api.routes.test_api_conversations import AGENT, _queued
from tests.unit.api.routes.test_api_current_plans import discover, publication


@pytest.fixture
def deliver(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr("agent_backbone.api.routes.conversations.deliver_now", mock)
    return mock


async def publish(client, headers, body):
    result = await client.post("/api/current-plans", headers=headers, json=body)
    assert result.status_code == 201, result.text
    return result.json()["receipt"]


@pytest.mark.parametrize(
    "runtime", ("claude", "codex", "opencode", "gemini", "deepcode", "aider", "shell")
)
async def test_plan_conversation_round_trip(api_app, api_client, auth_headers, deliver, runtime):
    db = api_app.state.db
    await db.agents.update_fields("ike", {"runtime": runtime})
    identity = (await discover(api_client, auth_headers))["author_id"]
    body = publication(identity)
    first = await publish(api_client, auth_headers, body)
    path = f"/api/conversations/plan/{identity}"
    item = body["plan"]["items"][0]
    ask = {
        "request_id": "question",
        "text": "What is left?",
        "anchor": {
            "revision_id": first["revision_id"],
            "item_id": item["item_id"],
            "label": item["text"],
        },
    }
    response = await api_client.post(path + "/messages", headers=auth_headers, json=ask)
    assert response.status_code == 201, response.text
    entry = response.json()["entry"]
    assert entry["anchor"]["context"]["item"] == item
    queued = await _queued(db, entry["id"])
    assert queued["registration_id"] == identity
    assert "Observed plan context (quoted data)" in queued["message"]
    assert f'"item_id": "{item["item_id"]}"' in queued["message"]
    assert "backbone conversations reply plan" in queued["message"]
    await db.queue.mark_delivered(queued["id"])

    changed = deepcopy(body)
    changed.update(request_id=uuid4().hex, base_revision_id=first["revision_id"])
    changed["plan"]["items"][0]["status"] = "done"
    second = await publish(api_client, auth_headers, changed)
    retry = await api_client.post(path + "/messages", headers=auth_headers, json=ask)
    assert retry.status_code == 200 and retry.json()["entry"]["id"] == entry["id"]
    assert deliver.await_count == 1
    stale = await api_client.post(
        path + "/messages", headers=auth_headers, json=ask | {"request_id": "stale"}
    )
    assert stale.status_code == 409 and stale.json()["error"] == "stale_revision"
    answered = await api_client.post(
        path + "/messages",
        headers=AGENT,
        json={
            "author": "ike",
            "request_id": "answer",
            "text": "Done.",
            "in_reply_to": entry["id"],
            "resulting_revision_id": second["revision_id"],
        },
    )
    assert answered.status_code == 201, answered.text
    page = (await api_client.get(path, headers=auth_headers)).json()
    assert [e["kind"] for e in page["entries"]] == [
        "revision",
        "message",
        "revision",
        "step",
        "message",
    ]
    step = next(e["step"] for e in page["entries"] if e["kind"] == "step")
    assert (
        step["item_id"] == item["item_id"]
        and step["status"] == "done"
        and step["title"] == item["text"]
    )
    assert page["latest_revision_id"] == second["revision_id"]
    assert page["entries"][1]["anchor"]["context"]["item"]["status"] == "in_progress"
    # A publication retry creates neither revision nor step duplicates.
    again = await api_client.post("/api/current-plans", headers=auth_headers, json=changed)
    assert again.status_code == 200
    assert (await api_client.get(path, headers=auth_headers)).json() == page
    mine = (await api_client.get("/api/conversations?agent=ike&kind=plan", headers=AGENT)).json()
    assert [i["artifact_id"] for i in mine["items"]] == [identity]
    assert (await api_client.get(path + "?agent=brunel", headers=AGENT)).status_code == 403

    versions = (
        await api_client.get(
            f"/api/current-plans/{identity}/versions?limit=1", headers=auth_headers
        )
    ).json()
    assert versions["items"][0]["revision_id"] == second["revision_id"]
    older = (
        await api_client.get(
            f"/api/current-plans/{identity}/versions",
            headers=auth_headers,
            params={"before": versions["next_before_id"]},
        )
    ).json()
    assert [v["revision_id"] for v in older["items"]] == [first["revision_id"]]
    assert not older["items"][0]["content_available"]
    lost = await api_client.get(
        f"/api/current-plans/{identity}/versions/{first['revision_id']}", headers=auth_headers
    )
    assert lost.status_code == 410 and lost.json()["detail"]["code"] == "version_not_retained"
    kept = await api_client.get(
        f"/api/current-plans/{identity}/versions/{second['revision_id']}", headers=auth_headers
    )
    assert kept.json()["plan"] == changed["plan"]
    async with db.engine.begin() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM current_plans")) == 1
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM message_queue WHERE source='conversation'")
            )
            == 1
        )


async def test_plan_thread_survives_replacement_and_retirement(
    api_app, api_client, auth_headers, deliver
):
    db = api_app.state.db
    identity = (await discover(api_client, auth_headers))["author_id"]
    body = publication(identity)
    first = await publish(api_client, auth_headers, body)
    path = f"/api/conversations/plan/{identity}"
    replacement = publication(identity)
    replacement["base_revision_id"] = first["revision_id"]
    await publish(api_client, auth_headers, replacement)
    await db.agents.rename("ike", "renamed")
    page = (await api_client.get(path, headers=auth_headers)).json()
    assert page["responsible"]["name"] == "renamed"
    assert [e["kind"] for e in page["entries"]] == ["revision", "revision"]
    await db.agents.delete("renamed")
    await db.current_plans.prune(30)
    await author(db, "ike")
    retired = (await api_client.get(path, headers=auth_headers)).json()
    assert retired["responsible"]["status"] == "retired"
    refuse = await api_client.post(
        path + "/messages", headers=auth_headers, json={"request_id": "q", "text": "Who?"}
    )
    assert refuse.status_code == 422 and refuse.json()["error"] == "recipient_required"
    explicit = await api_client.post(
        path + "/messages",
        headers=auth_headers,
        json={"request_id": "q", "text": "Who?", "recipient": "brunel"},
    )
    assert explicit.status_code == 201
    assert explicit.json()["entry"]["recipient"]["name"] == "brunel"
    # An addressed agent can discuss a retired plan, but cannot claim its revisions.
    bad = await api_client.post(
        path + "/messages",
        headers=AGENT,
        json={
            "author": "brunel",
            "request_id": "a",
            "text": "Mine",
            "resulting_revision_id": first["revision_id"],
        },
    )
    assert bad.status_code == 422 and bad.json()["error"] == "invalid_revision"
    assert (await api_client.get(path + "?agent=ike", headers=AGENT)).status_code == 403


async def test_pre_adapter_plan_baseline_and_transaction_rollback(
    api_app, api_client, auth_headers, deliver, monkeypatch
):
    db = api_app.state.db
    identity = (await discover(api_client, auth_headers))["author_id"]
    body = publication(identity)
    first = await publish(api_client, auth_headers, body)
    # Existing installation before this adapter has only its current row.
    async with db.engine.begin() as conn:
        await conn.execute(text("DELETE FROM conversation_entries"))
    path = f"/api/conversations/plan/{identity}"
    assert (await api_client.get(path, headers=auth_headers)).json()["entries"] == []
    bad_anchor = {
        "request_id": "bad",
        "text": "Missing?",
        "anchor": {"revision_id": first["revision_id"], "item_id": "missing"},
    }
    bad = await api_client.post(path + "/messages", headers=auth_headers, json=bad_anchor)
    assert bad.status_code == 422 and bad.json()["error"] == "invalid_anchor"
    assert (await api_client.get(path, headers=auth_headers)).json()["entries"] == []
    ask = {"request_id": "q", "text": "Explain", "anchor": {"revision_id": first["revision_id"]}}
    sent = await api_client.post(path + "/messages", headers=auth_headers, json=ask)
    assert sent.status_code == 201
    page = (await api_client.get(path, headers=auth_headers)).json()
    assert [e["kind"] for e in page["entries"]] == ["revision", "message"]
    # A failure while recording events rolls back the new checklist and receipt too.
    import agent_backbone.services.database._current_plans_repo as module

    original = module.published

    async def fail_after_events(*args):
        await original(*args)
        raise RuntimeError("event transaction failed")

    monkeypatch.setattr(module, "published", fail_after_events)
    changed = deepcopy(body)
    changed.update(request_id=uuid4().hex, base_revision_id=first["revision_id"])
    changed["plan"]["items"][0]["status"] = "done"
    with pytest.raises(RuntimeError, match="event transaction"):
        await publish(api_client, auth_headers, changed)
    assert (await api_client.get(path, headers=auth_headers)).json() == page
    current = (await api_client.get(f"/api/current-plans/{identity}", headers=auth_headers)).json()
    assert current["revision_id"] == first["revision_id"] and current["plan"] == body["plan"]


async def test_racing_plan_update_never_anchors_new_content_as_old(
    api_app, api_client, auth_headers, deliver
):
    import asyncio

    identity = (await discover(api_client, auth_headers))["author_id"]
    body = publication(identity)
    first = await publish(api_client, auth_headers, body)
    path = f"/api/conversations/plan/{identity}"
    changed = deepcopy(body)
    changed.update(request_id=uuid4().hex, base_revision_id=first["revision_id"])
    changed["plan"]["items"][0]["text"] = "Entirely changed meaning"
    ask = {
        "request_id": "race",
        "text": "Explain this",
        "anchor": {
            "revision_id": first["revision_id"],
            "item_id": body["plan"]["items"][0]["item_id"],
        },
    }
    sent, updated = await asyncio.gather(
        api_client.post(path + "/messages", headers=auth_headers, json=ask),
        api_client.post("/api/current-plans", headers=auth_headers, json=changed),
    )
    assert updated.status_code == 201
    assert sent.status_code in (201, 409)
    if sent.status_code == 201:
        assert sent.json()["entry"]["anchor"]["context"]["item"] == body["plan"]["items"][0]
    else:
        assert sent.json()["error"] == "stale_revision"
    async with api_app.state.db.engine.begin() as conn:
        count = await conn.scalar(
            text("SELECT count(*) FROM message_queue WHERE source='conversation'")
        )
    assert count == (1 if sent.status_code == 201 else 0)


def test_cli_displays_plan_step_event(capsys):
    from agent_backbone.cli.conversations import _print_conversation

    _print_conversation(
        {
            "kind": "plan",
            "artifact_id": "abc",
            "title": "Current work",
            "latest_revision_id": "new",
            "responsible": {"name": "worker", "status": "active"},
            "total": 0,
            "older": None,
            "entries": [
                {
                    "id": 1,
                    "kind": "step",
                    "author": {"name": "worker", "type": "agent"},
                    "step": {
                        "item_id": "item",
                        "title": "Verify the fix",
                        "status": "done",
                        "section": "now",
                    },
                }
            ],
        }
    )
    out = capsys.readouterr().out
    assert "Verify the fix: done (now)" in out


async def test_plan_reads_parse_only_a_bounded_page_of_revision_history(
    api_app, api_client, auth_headers, deliver, monkeypatch
):
    import json

    from sqlalchemy import insert

    from agent_backbone.services.database.models import ConversationEntryORM

    identity = (await discover(api_client, auth_headers))["author_id"]
    body = publication(identity)
    first = await publish(api_client, auth_headers, body)
    old_ids = [uuid4().hex for _ in range(500)]
    rows = [
        {
            "kind": "plan",
            "artifact_id": identity,
            "entry_kind": "revision",
            "created_at": first["updated_at"],
            "author_type": "agent",
            "author_id": identity,
            "author_name": "ike",
            "resulting_revision_id": rid,
            "detail": json.dumps(
                {
                    "revision_id": rid,
                    "label": f"rev {rid}",
                    "published_at": first["updated_at"],
                    "plan_id": body["plan"]["plan_id"],
                }
            ),
        }
        for rid in old_ids + [first["revision_id"]]
    ]
    async with api_app.state.db.engine.begin() as conn:
        await conn.execute(text("DELETE FROM conversation_entries"))
        await conn.execute(insert(ConversationEntryORM.__table__), rows)
    loads = json.loads
    parsed = []

    def counted(value, *args, **kwargs):
        if isinstance(value, str) and '"published_at"' in value:
            parsed.append(value)
        return loads(value, *args, **kwargs)

    monkeypatch.setattr(json, "loads", counted)
    path = f"/api/conversations/plan/{identity}"
    page = (await api_client.get(path + "?limit=5", headers=auth_headers)).json()
    assert len(page["entries"]) == 5
    versions = (
        await api_client.get(
            f"/api/current-plans/{identity}/versions?limit=5", headers=auth_headers
        )
    ).json()
    assert len(versions["items"]) == 5 and versions["has_more"]
    old = await api_client.get(
        f"/api/current-plans/{identity}/versions/{old_ids[0]}", headers=auth_headers
    )
    assert old.status_code == 410
    reply = await api_client.post(
        path + "/messages",
        headers=AGENT,
        json={
            "request_id": "old-result",
            "author": "ike",
            "text": "That earlier update was mine.",
            "resulting_revision_id": old_ids[0],
        },
    )
    assert reply.status_code == 201, reply.text
    # Amount decoded is bounded by the requested page/references, not 501 revisions per call.
    assert len(parsed) < 30


async def test_redelivered_plan_question_labels_its_observed_revision(
    api_app, api_client, auth_headers, deliver
):
    db = api_app.state.db
    identity = (await discover(api_client, auth_headers))["author_id"]
    body = publication(identity)
    first = await publish(api_client, auth_headers, body)
    path = f"/api/conversations/plan/{identity}"
    ask = {
        "request_id": "old-question",
        "text": "What remains?",
        "anchor": {
            "revision_id": first["revision_id"],
            "item_id": body["plan"]["items"][0]["item_id"],
        },
    }
    entry = (await api_client.post(path + "/messages", headers=auth_headers, json=ask)).json()[
        "entry"
    ]
    async with db.engine.begin() as conn:
        await conn.execute(
            text("UPDATE message_queue SET status='expired' WHERE source='conversation'")
        )
    changed = deepcopy(body)
    changed.update(request_id=uuid4().hex, base_revision_id=first["revision_id"])
    changed["plan"]["title"] = "Later work"
    changed["plan"]["items"][0]["status"] = "done"
    await publish(api_client, auth_headers, changed)
    sent = await api_client.post(
        path + f"/messages/{entry['id']}/redeliver",
        headers=auth_headers,
        json={"request_id": "resend"},
    )
    assert sent.json()["result"] == "queued"
    queued = await _queued(db, entry["id"])
    assert f"(rev {first['revision_id']})" in queued["message"]
    assert 'On "Current work"' in queued["message"]
    assert '"status": "in_progress"' in queued["message"]
    versions = (
        await api_client.get(f"/api/current-plans/{identity}/versions", headers=auth_headers)
    ).json()
    assert all(v["label"] == f"rev {v['revision_id']}" for v in versions["items"])
