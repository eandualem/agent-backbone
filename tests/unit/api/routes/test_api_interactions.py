"""Captured communication and owner discussion without model or terminal calls."""

import contextlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text

from agent_backbone.api.interaction_models import Interaction, InteractionPage, Summary
from agent_backbone.config import agent_api_key
from agent_backbone.services.database.models import InteractionORM
from agent_backbone.services.routing.models import SessionIntelligence, SessionProfile
from tests.conftest import TEST_API_KEY

WINDOW = {"since": "2020-01-01T00:00:00Z", "until": "2040-01-01T00:00:00Z"}


async def identity(db, name):
    async with db.engine.begin() as conn:
        return await conn.scalar(
            text("SELECT report_identity FROM agents WHERE name=:name"), {"name": name}
        )


async def records(db):
    async with db.engine.begin() as conn:
        return [dict(r) for r in (await conn.execute(select(InteractionORM.__table__))).mappings()]


@pytest.fixture
def transport(monkeypatch):
    ready = AsyncMock(
        return_value=SessionProfile(
            session_name="ike", intelligence=SessionIntelligence.READY, runtime="codex"
        )
    )
    send = AsyncMock(return_value=True)
    monkeypatch.setattr("agent_backbone.services.routing._delivery.get_session_intelligence", ready)
    monkeypatch.setattr("agent_backbone.services.routing._delivery.send_message", send)
    monkeypatch.setattr("agent_backbone.api.routes.conversations.deliver_now", AsyncMock())
    return ready, send


async def send(client, headers, **changes):
    return await client.post(
        "/api/messages",
        headers=headers,
        json={
            "target_session": "ike",
            "from_entity": "bell",
            "message": "Original body\n  whitespace retained",
            **changes,
        },
    )


@pytest.mark.parametrize(
    "runtime", ["claude", "codex", "opencode", "gemini", "deepcode", "aider", "shell"]
)
async def test_capture_immediate_original_and_read(
    api_app, api_client, auth_headers, transport, runtime
):
    transport[0].return_value.runtime = runtime
    result = await send(api_client, auth_headers)
    assert result.status_code == 200, result.text
    rows = await records(api_app.state.db)
    assert len(rows) == 1 and rows[0]["body"] == "Original body\n  whitespace retained"
    detail = await api_client.get("/api/interactions/" + rows[0]["id"], headers=auth_headers)
    assert detail.status_code == 200, detail.text
    record = Interaction.model_validate(detail.json())
    assert record.status.delivered and record.attempt_count == 1
    assert record.sender.attribution == "unverified"
    assert [a.name for a in record.conversation.recipients] == ["ike"]
    assert record.lifecycle.items[0].kind == "accepted"
    history = await api_client.get(
        "/api/interactions",
        headers=auth_headers,
        params=WINDOW | {"agent_id": rows[0]["recipient_id"]},
    )
    assert history.status_code == 200, history.text
    assert len(InteractionPage.model_validate(history.json()).items) == 1


async def test_queue_retry_one_original_and_ack(api_app, api_client, auth_headers, transport):
    db = api_app.state.db
    transport[0].return_value.intelligence = SessionIntelligence.AGENT_WORKING
    first = await send(api_client, auth_headers)
    second = await send(api_client, auth_headers)
    assert first.json()["operation_id"] == second.json()["operation_id"]
    rows = await records(db)
    assert len(rows) == 1
    before = await db.queue.by_operation(first.json()["operation_id"])
    # History must not claim queue work.
    await api_client.get("/api/interactions/" + rows[0]["id"], headers=auth_headers)
    assert (
        (await db.queue.by_operation(first.json()["operation_id"]))["status"]
        == before["status"]
        == "pending"
    )
    held = await db.queue.checkpoint("ike")
    await db.queue.acknowledge_checkpoint("ike", [held[0]["ack_token"]])
    detail = await db.interactions.detail(rows[0]["id"])
    assert detail["status"]["value"] == "acknowledged"
    assert any(e["kind"] == "acknowledged" for e in detail["lifecycle"]["items"])
    assert detail["body"]["text"] == rows[0]["body"]


async def test_owner_bound_identity_and_conversation_access(
    api_app, api_client, auth_headers, transport
):
    db = api_app.state.db
    bell = await identity(db, "bell")
    headers = {"Authorization": "Bearer " + agent_api_key(TEST_API_KEY, bell)}
    owner_refused = await send(api_client, headers, interaction={"actor": "owner"})
    assert owner_refused.status_code == 403
    result = await send(api_client, headers)
    assert result.status_code == 200, result.text
    row = (await records(db))[0]
    detail = await db.interactions.detail(row["id"])
    assert {a["name"] for a in detail["conversation"]["recipients"]} == {"bell", "ike"}
    path = "/api/conversations/interaction/" + row["id"]
    assert (await api_client.get(path, headers=headers)).status_code == 200
    assert (
        await api_client.get("/api/interactions/" + row["id"], headers=headers)
    ).status_code == 403
    legacy = {
        "Authorization": "Bearer " + agent_api_key(TEST_API_KEY),
        "X-Backbone-Client-Agent": "bell",
    }
    assert (await api_client.get(path, headers=legacy)).status_code == 403
    outsider = {
        "Authorization": "Bearer " + agent_api_key(TEST_API_KEY, await identity(db, "curie"))
    }
    assert (await api_client.get(path, headers=outsider)).status_code == 403
    refused = await api_client.post(
        path + "/messages",
        headers=auth_headers,
        json={"request_id": "bad", "recipient": "curie", "text": "question"},
    )
    assert refused.status_code == 403
    question = await api_client.post(
        path + "/messages",
        headers=auth_headers,
        json={"request_id": "ask", "recipient": "bell", "text": "Why this handoff?"},
    )
    assert question.status_code == 201, question.text
    # The other verified participant finds the discussion, not only the one addressed.
    ike = {"Authorization": "Bearer " + agent_api_key(TEST_API_KEY, await identity(db, "ike"))}
    listed = await api_client.get("/api/conversations", headers=ike)
    assert row["id"] in [item["artifact_id"] for item in listed.json()["items"]], listed.text
    replay = await api_client.post(
        path + "/messages",
        headers=auth_headers,
        json={"request_id": "ask", "recipient": "bell", "text": "Why this handoff?"},
    )
    assert replay.status_code == 200
    # Redelivery moves the question's entry to a new operation aliased to the original.
    from agent_backbone.services.database._interaction_capture import event

    question_op = (await records(db))[1]["operation_id"]
    async with db.engine.begin() as conn:
        original = await conn.scalar(
            text("SELECT id FROM interactions WHERE operation_id=:op"), {"op": question_op}
        )
        await event(
            conn, "redelivered-op", "attempt", outcome="attempting", interaction_id=original
        )
        await conn.execute(
            text("UPDATE conversation_entries SET operation_id='redelivered-op' WHERE id=:id"),
            {"id": question.json()["entry"]["id"]},
        )
    reply = await api_client.post(
        path + "/messages",
        headers=headers,
        json={
            "author": "bell",
            "request_id": "answer",
            "text": "Explanation now",
            "in_reply_to": question.json()["entry"]["id"],
        },
    )
    assert reply.status_code == 201, reply.text
    captured = await records(db)
    assert len(captured) == 3
    assert json.loads(captured[1]["sender"])["kind"] == "owner"
    assert any(r["type"] == "reply_to" for r in json.loads(captured[2]["relations"]))


async def test_summary_all_agents_empty_offline_and_owner(
    api_app, api_client, auth_headers, transport, monkeypatch
):
    await send(api_client, auth_headers, interaction={"actor": "owner"})
    monkeypatch.setattr(api_app.state.feed, "snapshot", AsyncMock(return_value=[]))
    result = await api_client.get("/api/interactions/summary", headers=auth_headers, params=WINDOW)
    assert result.status_code == 200, result.text
    summary = Summary.model_validate(result.json())
    byname = {s.agent.name: s for s in summary.items}
    assert len(byname) == 9
    assert byname["ike"].counts.received == 1
    assert byname["curie"].counts.sent == 0 and byname["curie"].latest is None
    assert byname["ike"].counterparties[0].kind == "owner"


async def test_lifecycle_overlap_cursor_scope_and_frozen_snapshot(
    api_app, api_client, auth_headers, transport
):
    db = api_app.state.db
    await send(api_client, auth_headers, message="before window")
    row = (await records(db))[0]
    async with db.engine.begin() as conn:
        await conn.execute(
            text("UPDATE interactions SET accepted_at='2021-01-01T00:00:00.000000Z' WHERE id=:id"),
            {"id": row["id"]},
        )
        await conn.execute(
            text(
                "UPDATE interaction_events SET time='2021-01-01T00:00:00.000000Z' "
                "WHERE kind='accepted'"
            )
        )
    params = {
        "since": "2025-01-01T00:00:00Z",
        "until": "2040-01-01T00:00:00Z",
        "agent_id": row["recipient_id"],
        "limit": 1,
    }
    await send(api_client, auth_headers, message="second message")
    first = await api_client.get("/api/interactions", headers=auth_headers, params=params)
    assert first.status_code == 200, first.text
    assert first.json()["next_cursor"]
    for subpage in ("attempts", "events"):  # a history cursor is refused, never a 500
        wrong = await api_client.get(
            f"/api/interactions/{row['id']}/{subpage}",
            headers=auth_headers,
            params={"cursor": first.json()["next_cursor"]},
        )
        assert wrong.status_code == 422, wrong.text
    await send(api_client, auth_headers, message="after snapshot")
    second = await api_client.get(
        "/api/interactions",
        headers=auth_headers,
        params=params | {"cursor": first.json()["next_cursor"]},
    )
    assert second.status_code == 200, second.text
    assert second.json()["items"][0]["body"]["text"] == "before window"
    assert second.json()["next_cursor"] is None
    bad = await api_client.get(
        "/api/interactions",
        headers=auth_headers,
        params=params | {"direction": "outgoing", "cursor": first.json()["next_cursor"]},
    )
    assert bad.status_code == 422


async def test_retention_keeps_anchored_original_and_does_not_rebind(
    api_app, api_client, auth_headers, transport
):
    db = api_app.state.db
    await send(api_client, auth_headers, interaction={"actor": "owner"})
    row = (await records(db))[0]
    path = "/api/conversations/interaction/" + row["id"]
    response = await api_client.post(
        path + "/messages",
        headers=auth_headers,
        json={"request_id": "ask", "text": "Keep context", "recipient": "ike"},
    )
    assert response.status_code == 201, response.text
    async with db.engine.begin() as conn:
        await conn.execute(text("UPDATE interactions SET accepted_at='2000-01-01T00:00:00+00:00'"))
    await db.interactions.prune(30)
    assert (await db.interactions.detail(row["id"]))["body"]["level"] == "full"
    await db.agents.rename("ike", "new-name")
    detail = await db.interactions.detail(row["id"])
    assert (
        detail["recipient"]["name"] == "new-name"
        and detail["recipient"]["name_at_capture"] == "ike"
    )


def test_committed_wire_fixtures():
    p = Path(__file__).parents[4] / "docs/contracts/interactions-v1"
    for name, model in [
        ("history", InteractionPage),
        ("detail", Interaction),
        ("summary", Summary),
    ]:
        parsed = model.model_validate_json((p / f"{name}.fixture.json").read_text())
        if name == "summary":  # an untruncated timeline shows every counted interaction
            for item in parsed.items:
                if not item.ticks_truncated:
                    assert len(item.ticks) == item.counts.sent + item.counts.received
                start = parsed.coverage.capture_started_at
                assert all(tick.time >= start for tick in item.ticks)  # nothing predates capture
                order = [(tick.time, tick.interaction_id) for tick in item.ticks]
                assert order == sorted(order, reverse=True)  # newest first, as summary emits
        assert (
            json.loads((p / f"{model.__name__}.schema.json").read_text())
            == model.model_json_schema()
        )


async def test_failed_retry_keeps_original_and_capture_failure_prevents_paste(
    api_app, api_client, auth_headers, transport, monkeypatch
):
    transport[1].return_value = False
    a = await send(api_client, auth_headers)
    b = await send(api_client, auth_headers)
    assert a.json()["operation_id"] == b.json()["operation_id"]
    assert len(await records(api_app.state.db)) == 1
    transport[1].reset_mock()
    monkeypatch.setattr(
        "agent_backbone.services.database._interaction_capture._capture",
        AsyncMock(side_effect=OSError("full")),
    )
    with pytest.raises(RuntimeError, match="interaction capture failed"):
        await send(api_client, auth_headers, message="new independent message")
    transport[1].assert_not_awaited()


async def test_explicit_relations_validate_targets_and_unknown_sender_stays_unknown(
    api_app, api_client, auth_headers, transport
):
    await send(api_client, auth_headers, interaction={"actor": "owner"})
    original = (await records(api_app.state.db))[0]
    relation = {
        "type": "for_owner_request",
        "target": {"kind": "owner_request", "id": original["id"]},
    }
    linked = await send(
        api_client, auth_headers, message="Follow through", interaction={"relations": [relation]}
    )
    assert linked.status_code == 200, linked.text
    assert (
        json.loads((await records(api_app.state.db))[1]["relations"])[0]["target"]["id"]
        == original["id"]
    )
    relation["target"]["id"] = "f" * 32
    missing = await send(
        api_client, auth_headers, message="No such target", interaction={"relations": [relation]}
    )
    assert missing.status_code == 422
    await send(api_client, auth_headers, from_entity="not-a-registration", message="Unknown source")
    last = (await records(api_app.state.db))[-1]
    assert json.loads(last["sender"])["id"] is None
    assert json.loads(last["sender"])["kind"] == "unknown"


async def test_retirement_name_reuse_cannot_read_or_receive_old_discussion(
    api_app, api_client, auth_headers, transport
):
    db = api_app.state.db
    old = await identity(db, "bell")
    headers = {"Authorization": "Bearer " + agent_api_key(TEST_API_KEY, old)}
    await send(api_client, headers)
    row = (await records(db))[0]
    await db.agents.delete("bell")
    await db.agents.upsert(
        "bell",
        dir="/fixture/new",
        unattended=False,
        runtime="codex",
        model=None,
        repo="",
        tags=[],
        env={},
        description="",
    )
    new = await identity(db, "bell")
    assert new != old
    path = "/api/conversations/interaction/" + row["id"]
    read = await api_client.get(
        path, headers={"Authorization": "Bearer " + agent_api_key(TEST_API_KEY, new)}
    )
    assert read.status_code == 403
    ask = await api_client.post(
        path + "/messages",
        headers=auth_headers,
        json={"request_id": "retired", "recipient": "bell", "text": "Do not rebind"},
    )
    assert ask.status_code == 403
    old_ask = await api_client.post(
        path + "/messages",
        headers=auth_headers,
        json={"request_id": "retired-id", "recipient": old, "text": "Do not route"},
    )
    assert old_ask.status_code == 422
    detail = await db.interactions.detail(row["id"])
    assert detail["sender"]["retired"]
    assert detail["sender"]["id"] == old


async def test_subpage_cursor_is_bound_and_unanchored_retention_is_explicit(
    api_app, api_client, auth_headers, transport
):
    db = api_app.state.db
    await send(api_client, auth_headers)
    row = (await records(db))[0]
    for n in range(55):
        await db.deliveries.record(
            issue_number=None,
            target_entity="ike",
            session_name="ike",
            outcome="delivered",
            operation_id=row["operation_id"],
            source=f"fixture-{n}",
        )
    detail = await db.interactions.detail(row["id"])
    assert detail["attempt_count"] == 56 and len(detail["attempts"]["items"]) == 50
    page = await db.interactions.subpage(row["id"], "attempts", detail["attempts"]["next_cursor"])
    assert len(page["items"]) == 6 and page["next_cursor"] is None
    with pytest.raises(ValueError):
        await db.interactions.subpage(row["id"], "events", detail["attempts"]["next_cursor"])
    async with db.engine.begin() as conn:
        await conn.execute(
            text("UPDATE interactions SET accepted_at='2000-01-01T00:00:00.000000Z'")
        )
    await db.interactions.prune(30)
    pruned = await db.interactions.detail(row["id"])
    assert pruned["body"]["level"] == "excerpt" and pruned["body"]["reason"] == "retention"


async def test_explicit_redelivery_is_one_logical_message(
    api_app, api_client, auth_headers, transport
):
    db = api_app.state.db
    await send(api_client, auth_headers, interaction={"actor": "owner"})
    original = (await records(db))[0]
    path = "/api/conversations/interaction/" + original["id"]
    question = await api_client.post(
        path + "/messages",
        headers=auth_headers,
        json={"request_id": "ask-retry", "recipient": "ike", "text": "Ask once"},
    )
    entry = question.json()["entry"]
    captured = await records(db)
    question_record = next(r for r in captured if r["body"] == "Ask once")
    async with db.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE message_queue SET status='expired',"
                "delivered_at='2026-10-31T00:00:00.000000Z' WHERE operation_id=:op"
            ),
            {"op": question_record["operation_id"]},
        )
    retry = await api_client.post(
        path + f"/messages/{entry['id']}/redeliver",
        headers=auth_headers,
        json={"request_id": "retry-1"},
    )
    assert retry.status_code == 200, retry.text
    async with db.engine.begin() as conn:
        new_op = await conn.scalar(
            text("SELECT operation_id FROM conversation_entries WHERE id=:id"), {"id": entry["id"]}
        )
    assert new_op != question_record["operation_id"]
    assert len(await records(db)) == 2
    await db.deliveries.record(
        issue_number=None,
        target_entity="ike",
        session_name="ike",
        outcome="delivered",
        operation_id=new_op,
        source="fixture-redelivery",
    )
    detail = await db.interactions.detail(question_record["id"])
    assert detail["status"]["delivered"]
    assert detail["attempts"]["items"][0]["operation_id"] == new_op
    assert detail["body"]["text"] == "Ask once"


async def test_unbound_sender_cannot_validate_links_to_unknown_actor(
    api_app, api_client, auth_headers, transport
):
    await send(api_client, auth_headers, from_entity="unknown", message="unknown actor")
    row = (await records(api_app.state.db))[0]
    legacy = {"Authorization": "Bearer " + agent_api_key(TEST_API_KEY)}
    response = await send(
        api_client,
        legacy,
        message="not a verified participant",
        interaction={
            "relations": [{"type": "reply_to", "target": {"kind": "interaction", "id": row["id"]}}]
        },
    )
    assert response.status_code == 422


async def test_reverse_links_are_recorded_and_conversation_exposes_original_to_participant(
    api_app, api_client, auth_headers, transport
):
    db = api_app.state.db
    bell = await identity(db, "bell")
    headers = {"Authorization": "Bearer " + agent_api_key(TEST_API_KEY, bell)}
    await send(
        api_client,
        auth_headers,
        target_session="bell",
        interaction={"actor": "owner"},
        message="Original owner request",
    )
    original = (await records(db))[0]
    await send(
        api_client,
        headers,
        message="Explicitly linked",
        interaction={
            "relations": [
                {
                    "type": "for_owner_request",
                    "target": {"kind": "owner_request", "id": original["id"]},
                }
            ]
        },
    )
    linked = (await records(db))[1]
    detail = await db.interactions.detail(original["id"])
    assert detail["relations"][0]["source_interaction_id"] == linked["id"]
    assert detail["relations"][0]["target"]["id"] == original["id"]
    path = "/api/conversations/interaction/" + linked["id"]
    page = await api_client.get(path, headers=headers)
    assert page.status_code == 200, page.text
    assert page.json()["original_context"]["body"]["text"] == "Explicitly linked"
    refuse = await api_client.post(
        path + "/messages",
        headers=headers,
        json={
            "author": "bell",
            "request_id": "no-edit",
            "text": "Cannot revise it",
            "resulting_revision_id": linked["id"],
        },
    )
    assert refuse.status_code == 422


async def test_historical_gap_excludes_later_retry_but_includes_pending_span(
    api_app, api_client, auth_headers, transport, monkeypatch
):
    monkeypatch.setattr(api_app.state.feed, "snapshot", AsyncMock(return_value=[]))
    db = api_app.state.db
    await send(api_client, auth_headers, message="Historical lifecycle")
    row = (await records(db))[0]
    async with db.engine.begin() as conn:
        await conn.execute(
            text("UPDATE interactions SET accepted_at='2021-01-01T00:00:00.000000Z'")
        )
        await conn.execute(text("UPDATE interaction_events SET time='2021-01-01T00:00:00.000000Z'"))
    await db.interactions.observe(
        row["operation_id"],
        "queued",
        outcome="pending",
        source="fixture",
        at="2030-01-01T00:00:00.000000Z",
        time_basis="recorded",
    )
    params = {
        "since": "2025-01-01T00:00:00Z",
        "until": "2026-01-01T00:00:00Z",
        "agent_id": row["recipient_id"],
    }
    page = await api_client.get("/api/interactions", headers=auth_headers, params=params)
    assert page.status_code == 200, page.text
    assert page.json()["items"] == []
    summary = await api_client.get("/api/interactions/summary", headers=auth_headers, params=params)
    assert summary.json()["items"][0]["latest"] is None
    assert summary.json()["items"][0]["previous"]["id"] == row["id"]
    # If the earlier delivery was pending, it really did span the historical window.
    async with db.engine.begin() as conn:
        await conn.execute(
            text("UPDATE interaction_events SET outcome='pending' WHERE outcome='delivered'")
        )
    page = await api_client.get("/api/interactions", headers=auth_headers, params=params)
    assert [i["id"] for i in page.json()["items"]] == [row["id"]]


async def test_publication_attribution_preserves_bound_and_legacy_distinction(
    api_app, api_client, auth_headers
):
    from tests.unit.api.routes.test_api_responses import body

    db = api_app.state.db
    first = await api_client.post("/api/responses", headers=auth_headers, json=body())
    assert first.status_code == 201, first.text
    original = (await records(db))[0]
    assert json.loads(original["sender"])["attribution"] == "unverified"
    detail = await db.interactions.detail(original["id"])
    assert detail["conversation"]["recipients"] == []
    assert detail["relations"][0]["target"]["artifact_kind"] == "response"
    headers = {"Authorization": "Bearer " + agent_api_key(TEST_API_KEY, await identity(db, "ike"))}
    revised = await api_client.post(
        "/api/responses",
        headers=headers,
        json=body(key="revised", supersedes_id=first.json()["record"]["id"]),
    )
    assert revised.status_code == 201, revised.text
    correction = (await records(db))[1]
    assert json.loads(correction["sender"])["attribution"] == "credential_bound"
    assert json.loads(correction["body"])["answer"].endswith("revised")
    detail = await db.interactions.detail(correction["id"])
    assert [a["name"] for a in detail["conversation"]["recipients"]] == ["ike"]
    assert any(
        r["type"] == "corrects" and r["target"]["id"] == str(first.json()["record"]["id"])
        for r in detail["relations"]
    )


async def test_late_finalize_does_not_invent_a_transport_transition(
    api_app, api_client, auth_headers, transport
):
    db = api_app.state.db
    await send(api_client, auth_headers)
    row = (await records(db))[0]
    before = await db.interactions.detail(row["id"])
    delivery = before["attempts"]["items"][0]["delivery_id"]
    await db.deliveries.finalize(delivery, "failed", operation_id=row["operation_id"])
    after = await db.interactions.detail(row["id"])
    assert after["lifecycle"] == before["lifecycle"]
    assert after["attempts"] == before["attempts"]


async def test_issue_retirement_is_a_recorded_terminal_event(
    api_app, api_client, auth_headers, transport
):
    db = api_app.state.db
    transport[0].return_value.intelligence = SessionIntelligence.AGENT_WORKING
    await send(api_client, auth_headers)
    row = (await records(db))[0]
    delivery = await db.deliveries.record(
        issue_number=123,
        repo="example/fixture",
        target_entity="ike",
        session_name="ike",
        outcome="offline",
        operation_id=row["operation_id"],
        source="fixture",
        kind="issue",
    )
    await db.deliveries.retire(delivery, "issue_closed")
    detail = await db.interactions.detail(row["id"])
    assert detail["status"]["value"] == "issue_closed"
    assert not detail["status"]["delivered"]
    assert detail["lifecycle"]["items"][-1]["kind"] == "retired_issue_closed"


async def test_a_reused_delivery_id_keeps_its_own_attempt(db):
    for operation in ("op-first", "op-second"):
        await db.interactions.capture(
            operation_id=operation, message="hi", sender="bell", recipient="ike"
        )
    first = await db.deliveries.record(
        issue_number=None,
        target_entity="ike",
        session_name="ike",
        outcome="delivered",
        kind="direct_message",
        operation_id="op-first",
    )
    async with db.engine.begin() as conn:  # retention removed every delivery row
        await conn.execute(text("DELETE FROM deliveries"))
    second = await db.deliveries.record(
        issue_number=None,
        target_entity="ike",
        session_name="ike",
        outcome="delivered",
        kind="direct_message",
        operation_id="op-second",
    )
    assert second == first
    async with db.engine.begin() as conn:
        rows = (
            await conn.execute(
                text("SELECT operation_id FROM interaction_events WHERE delivery_id=:id"),
                {"id": second},
            )
        ).fetchall()
    assert sorted(row.operation_id for row in rows) == ["op-first", "op-second"]


@pytest.mark.parametrize(
    ("step", "outcome"),
    [("finalize", "delivered"), ("settle", "handed_off"), ("retire", "issue_closed")],
)
async def test_a_reused_delivery_id_keeps_its_own_final_outcome(db, step, outcome):
    claims = []
    for operation in ("op-first", "op-second"):
        await db.interactions.capture(
            operation_id=operation, message="hi", sender="bell", recipient="ike"
        )
        claimed = await db.deliveries.claim(
            issue_number=7,
            target_entity="ike",
            session_name="ike",
            source="test",
            operation_id=operation,
        )
        if step == "finalize":
            await db.deliveries.finalize(claimed, "delivered")
        elif step == "settle":
            await db.deliveries.finalize(claimed, "offered")
            assert await db.deliveries.settle(claimed, "handed_off", expected="offered")
        else:
            await db.deliveries.finalize(claimed, "delivery_failed")
            await db.deliveries.retire(claimed, "issue_closed")
        claims.append(claimed)
        async with db.engine.begin() as conn:  # retention removed every delivery row
            await conn.execute(text("DELETE FROM deliveries"))
    assert claims[0] == claims[1]
    async with db.engine.begin() as conn:
        rows = (
            await conn.execute(
                text("SELECT operation_id FROM interaction_events WHERE outcome=:outcome"),
                {"outcome": outcome},
            )
        ).fetchall()
    assert sorted(row.operation_id for row in rows) == ["op-first", "op-second"]


async def test_retiring_a_registration_ends_its_queued_interactions(api_app):
    db = api_app.state.db
    queued = await db.queue.enqueue(
        session_name="ike", message="still waiting", sender="bell", delivery_kind="direct_message"
    )
    assert await db.agents.delete("ike")
    async with db.engine.begin() as conn:
        interaction = await conn.scalar(
            text("SELECT id FROM interactions WHERE operation_id=:op"),
            {"op": queued.operation_id},
        )
    record = await db.interactions.detail(interaction)
    assert record["status"]["value"] == "registration_retired"


async def test_a_name_moved_mid_request_never_authorizes_another_registration(
    api_app, api_client, transport, monkeypatch
):
    from agent_backbone.services.database import _conversations_repo

    db = api_app.state.db
    bell, ike = await identity(db, "bell"), await identity(db, "ike")
    headers = {"Authorization": "Bearer " + agent_api_key(TEST_API_KEY, bell)}
    assert (await send(api_client, headers)).status_code == 200
    path = "/api/conversations/interaction/" + (await records(db))[0]["id"]
    assert (await api_client.get(path, headers=headers)).status_code == 200
    # Between authentication and the read, the name resolves to another participant.
    monkeypatch.setattr(_conversations_repo, "_registration", AsyncMock(return_value=ike))
    assert (await api_client.get(path, headers=headers)).status_code == 403


async def test_a_failed_capture_never_logs_the_message(
    api_app, api_client, auth_headers, transport, monkeypatch, caplog
):
    leak = RuntimeError("INSERT INTO interactions ... 'Original body'")
    monkeypatch.setattr(
        "agent_backbone.services.database._interaction_capture._capture",
        AsyncMock(side_effect=leak),
    )
    with caplog.at_level("DEBUG"), contextlib.suppress(RuntimeError):
        await send(api_client, auth_headers)  # the test client re-raises the 500
    assert "Original body" not in caplog.text


async def test_an_out_of_range_report_relation_is_an_absent_target(
    api_app, api_client, auth_headers, transport
):
    for number in (2**31, 2**63):  # past PostgreSQL's int4 and past SQLite's integer
        huge = {"type": "from_report", "target": {"kind": "report", "id": str(number)}}
        resp = await send(api_client, auth_headers, interaction={"relations": [huge]})
        assert resp.status_code == 422, resp.text
    entry = {"kind": "conversation", "artifact_kind": "report", "id": "1", "entry_id": 2**64}
    asked = {"type": "asked_in_conversation", "target": entry}
    resp = await send(api_client, auth_headers, interaction={"relations": [asked]})
    assert resp.status_code == 422, resp.text
