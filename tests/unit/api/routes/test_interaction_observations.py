"""Historical observations retain identity and never manufacture quiet or state."""

from unittest.mock import patch

import pytest
from sqlalchemy import text

from agent_backbone.api.interaction_models import StateSpanPage
from tests.unit.api.routes.test_api_interactions import (  # noqa: F401
    identity,
    records,
    send,
)
from tests.unit.api.routes.test_api_interactions import (
    transport as transport,
)

BASE = "2001-01-01T00:"


def at(minutes, seconds=0):
    return f"{BASE}{minutes:02d}:{seconds:02d}.000000Z"


async def sample(db, name, agent_id, minute, state="busy", recorder="one", second=0):
    with patch(
        "agent_backbone.services.database._state_history_repo.now_iso",
        return_value=at(minute, second),
    ):
        await db.state_history.observe_registered(
            [(name, state, None)],
            registrations={name: agent_id},
            evidence={name: ["fixture hook state"]},
            recorder_id=recorder,
        )


async def checkpoint(db, minute, recorder="one"):
    with patch(
        "agent_backbone.services.database._interaction_observations.now_iso",
        return_value=at(minute),
    ):
        await db.interactions.checkpoint(recorder)


@pytest.mark.parametrize(
    "runtime", ["claude", "codex", "opencode", "gemini", "deepcode", "aider", "shell"]
)
async def test_registered_samples_bound_spans_gaps_and_snapshot(
    api_app, api_client, auth_headers, runtime
):
    db = api_app.state.db
    await db.agents.update_fields("ike", {"runtime": runtime})
    agent = await identity(db, "ike")
    await sample(db, "ike", agent, 0)
    await sample(db, "ike", agent, 0, second=30)  # unchanged samples have a one-minute bound
    await sample(db, "ike", agent, 1)
    await sample(db, "ike", agent, 2, "idle")
    await sample(db, "ike", agent, 3, "idle")
    await sample(db, "ike", agent, 10, "idle")  # unobserved gap
    await sample(db, "ike", agent, 11, "idle", recorder="two")  # restart boundary
    params = {"agent_id": agent, "since": at(0), "until": at(12), "limit": 1}
    first = await api_client.get(
        "/api/interactions/state-spans", headers=auth_headers, params=params
    )
    assert first.status_code == 200, first.text
    page = StateSpanPage.model_validate(first.json())
    assert page.items[0].since == at(0) and page.items[0].until == at(1)
    assert page.items[0].basis == "matching_samples"
    assert page.coverage.completeness == "sampled_partial"
    await sample(db, "ike", agent, 11, "busy", recorder="two", second=30)
    items = list(page.items)
    cursor = page.next_cursor
    while cursor:
        response = await api_client.get(
            "/api/interactions/state-spans",
            headers=auth_headers,
            params=params | {"cursor": cursor},
        )
        assert response.status_code == 200, response.text
        page = StateSpanPage.model_validate(response.json())
        items.extend(page.items)
        cursor = page.next_cursor
    assert len(items) == 6
    assert items[1].since == items[1].until == at(1)  # transition time is not measured
    assert items[2].since == at(2) and items[2].until == at(3)
    assert all(i.basis == "point_observation" for i in items[3:])
    bad = await api_client.get(
        "/api/interactions/state-spans",
        headers=auth_headers,
        params=params
        | {"agent_id": await identity(db, "bell"), "cursor": first.json()["next_cursor"]},
    )
    assert bad.status_code == 422


async def test_no_legacy_name_rejoin_and_rename_retirement_are_stable(api_app):
    db = api_app.state.db
    old = await identity(db, "ike")
    await db.state_history.observe([("ike", "busy", None)])
    empty = await db.interactions.state_spans(agent_id=old, since=at(0), until=at(12))
    assert empty["items"] == [] and empty["coverage"]["completeness"] == "unavailable"
    await sample(db, "ike", old, 0)
    await db.agents.rename("ike", "renamed")
    await sample(db, "ike", old, 1)  # stale snapshot cannot bind by an old name
    await sample(db, "renamed", old, 2)
    await db.agents.delete("renamed")
    await db.agents.upsert(
        "ike",
        dir="/tmp/fixture",
        runtime="codex",
        model=None,
        repo="",
        tags=[],
        env={},
        description="",
        unattended=False,
    )
    new = await identity(db, "ike")
    assert new != old
    await sample(db, "ike", new, 3)
    old_page = await db.interactions.state_spans(agent_id=old, since=at(0), until=at(12))
    new_page = await db.interactions.state_spans(agent_id=new, since=at(0), until=at(12))
    assert [i["name_at_observation"] for i in old_page["items"]] == ["ike", "renamed"]
    assert len(new_page["items"]) == 1 and new_page["items"][0]["first_observed_at"] == at(3)


async def test_complete_message_interval_is_independent_of_sampled_state(api_app):
    db = api_app.state.db
    for minute in (0, 1, 2):
        await checkpoint(db, minute)
    result = await db.interactions.summary(since=at(0, 30), until=at(1, 30))
    c = result["coverage"]
    assert c["completeness"] == "complete_within_declared_scope"
    assert c["message_intervals"] == [
        {"since": at(0, 30), "until": at(1, 30), "completeness": "complete_within_declared_scope"}
    ]
    assert c["state_history"] == "observed_partial"
    states = await db.interactions.state_spans(
        agent_id=await identity(db, "ike"), since=at(0), until=at(2)
    )
    assert states["coverage"]["completeness"] == "unavailable"
    tail = await db.interactions.summary(since=at(0), until=at(3))
    assert tail["coverage"]["completeness"] == "partial"
    assert tail["coverage"]["message_intervals"][0]["until"] == at(2)


@pytest.mark.parametrize("gap", ["restart", "long_gap", "uncaptured"])
async def test_capture_gaps_never_certify_a_quiet_window(api_app, gap):
    db = api_app.state.db
    await checkpoint(db, 0)
    if gap == "uncaptured":
        await db.interactions.observe("missing-original", "attempt", outcome="delivered", at=at(1))
    await checkpoint(
        db, 3 if gap == "long_gap" else 2, recorder="two" if gap == "restart" else "one"
    )
    result = await db.interactions.summary(since=at(0), until=at(2))
    assert result["coverage"]["completeness"] == "partial"
    assert result["coverage"]["message_intervals"] == []


async def test_a_clock_rollback_never_joins_checkpoints_out_of_recording_order(api_app):
    db = api_app.state.db
    for minute in (2, 0, 1):  # the clock went back after the first checkpoint
        await checkpoint(db, minute)
    result = await db.interactions.summary(since=at(0), until=at(2))
    assert result["coverage"]["message_intervals"] == []


async def test_a_clock_rollback_never_joins_state_samples(api_app):
    db = api_app.state.db
    agent = await identity(db, "ike")
    # The clock went back after the first sample: busy at 2, then idle at 0, busy at 1.
    for minute, state in ((2, "busy"), (0, "idle"), (1, "busy")):
        await sample(db, "ike", agent, minute, state=state)
    spans = await db.interactions.state_spans(agent_id=agent, since=at(0), until=at(3))
    assert all(span["basis"] == "point_observation" for span in spans["items"])


async def test_legacy_pending_and_retention_do_not_certify_coverage(api_app):
    db = api_app.state.db
    await checkpoint(db, 0)
    await db.queue.enqueue(
        session_name="ike", message="legacy fixture", sender="bell", source="fixture"
    )
    async with db.engine.begin() as conn:
        await conn.execute(text("DELETE FROM interaction_events"))
        await conn.execute(text("DELETE FROM interactions"))
    await checkpoint(db, 1)
    result = await db.interactions.summary(since=at(0), until=at(1))
    assert result["coverage"]["completeness"] == "partial"
    assert result["coverage"]["message_intervals"] == []
    await db.state_history.prune()
    result = await db.interactions.summary(since=at(0), until=at(1))
    assert result["coverage"]["message_intervals"] == []


@pytest.mark.usefixtures("transport")
async def test_discussion_metadata_uses_real_entries_and_keeps_interleaving(
    api_app, api_client, auth_headers
):
    db = api_app.state.db
    await send(api_client, auth_headers, message="A", interaction={"actor": "owner"})
    await send(api_client, auth_headers, message="B", interaction={"actor": "owner"})
    originals = await records(db)
    entries = []
    for n, index in enumerate((0, 1, 0)):
        response = await api_client.post(
            "/api/conversations/interaction/" + originals[index]["id"] + "/messages",
            headers=auth_headers,
            json={"request_id": f"question-{n}", "text": f"question {n}", "recipient": "ike"},
        )
        assert response.status_code == 201, response.text
        entries.append(response.json()["entry"])
    captured = (await records(db))[2:]
    details = [await db.interactions.detail(row["id"]) for row in captured]
    metadata = [d["conversation_entry"] for d in details]
    assert [m["entry_id"] for m in metadata] == [e["id"] for e in entries]
    assert [m["created_at"] for m in metadata] == [e["created_at"] for e in entries]
    assert [m["anchored_interaction_id"] for m in metadata] == [
        originals[i]["id"] for i in (0, 1, 0)
    ]
    recipient = await identity(db, "ike")
    assert all(m["author_type"] == "owner" and m["recipient_id"] == recipient for m in metadata)
    # A sender-supplied relation is not provenance that it IS an existing entry.
    await send(
        api_client,
        auth_headers,
        message="Reference only",
        interaction={
            "relations": [
                {
                    "type": "asked_in_conversation",
                    "target": {
                        "kind": "conversation",
                        "id": originals[0]["id"],
                        "artifact_kind": "interaction",
                        "entry_id": entries[0]["id"],
                    },
                }
            ]
        },
    )
    assert (await db.interactions.detail((await records(db))[-1]["id"]))[
        "conversation_entry"
    ] is None


async def test_unknown_actor_does_not_certify_quiet_but_preserves_known_prefix(api_app):
    db = api_app.state.db
    for minute in (0, 1, 2, 3):
        await checkpoint(db, minute)
    await db.interactions.capture(
        operation_id="unknown-fixture",
        message="illustrative unknown sender",
        sender="not-recorded",
        recipient="ike",
        metadata={"accepted_at": at(1)},
    )
    await db.interactions.observe("unknown-fixture", "attempt", outcome="delivered", at=at(1, 30))
    c = (await db.interactions.summary(since=at(0), until=at(3)))["coverage"]
    assert c["completeness"] == "partial"
    assert "unverified_actor_attribution" in c["reasons"]
    assert c["capture_started_at"] == at(0)  # the gap never replaces the capture boundary
    assert [(i["since"], i["until"]) for i in c["message_intervals"]] == [
        (at(0), at(1)),
        (at(1, 30), at(3)),
    ]


async def test_all_agent_state_page_is_bounded_and_does_not_join_other_agents(
    api_app, api_client, auth_headers
):
    db = api_app.state.db
    ike, bell = await identity(db, "ike"), await identity(db, "bell")
    for minute in (0, 1):
        await sample(db, "ike", ike, minute, "busy")
        await sample(db, "bell", bell, minute, "idle")
    params = {"since": at(0), "until": at(2), "limit": 1}
    first = await api_client.get(
        "/api/interactions/state-spans", headers=auth_headers, params=params
    )
    assert first.status_code == 200, first.text
    assert len(first.json()["items"]) == 1 and first.json()["items"][0]["agent_id"] == ike
    second = await api_client.get(
        "/api/interactions/state-spans",
        headers=auth_headers,
        params=params | {"cursor": first.json()["next_cursor"]},
    )
    assert second.status_code == 200, second.text
    assert second.json()["items"][0]["agent_id"] == bell
    assert second.json()["items"][0]["state"] == "idle"
    assert second.json()["items"][0]["until"] == at(1)
    mismatch = await api_client.get(
        "/api/interactions/state-spans",
        headers=auth_headers,
        params=params | {"agent_id": ike, "cursor": first.json()["next_cursor"]},
    )
    assert mismatch.status_code == 422


async def test_clock_regression_is_not_a_capture_interval(api_app):
    db = api_app.state.db
    await checkpoint(db, 2)
    await checkpoint(db, 0)
    coverage = (await db.interactions.summary(since=at(0), until=at(2)))["coverage"]
    assert coverage["message_intervals"] == []
    assert "clock_regressed" in coverage["reasons"]


def test_additive_wire_fixtures_match_schema():
    import json
    from pathlib import Path

    from agent_backbone.api.interaction_models import ConversationEntryMetadata

    root = Path(__file__).parents[4] / "docs/contracts/interactions-v1"
    for name, model in (
        ("state-spans", StateSpanPage),
        ("conversation-entry", ConversationEntryMetadata),
    ):
        model.model_validate_json((root / (name + ".fixture.json")).read_text())
        assert (
            json.loads((root / (model.__name__ + ".schema.json")).read_text())
            == model.model_json_schema()
        )


async def test_session_feed_captures_identity_before_read_and_rejects_stale_registration(api_app):
    from dataclasses import replace
    from unittest.mock import AsyncMock

    from agent_backbone.api.session_updates import SessionFeed
    from agent_backbone.config import agents_from_rows
    from agent_backbone.services.agents import EnrichedAgent

    db = api_app.state.db
    old = await identity(db, "ike")
    config = replace(api_app.state.config, agents=agents_from_rows(await db.agents.list()))
    feed = SessionFeed(lambda: config, db=db, ttl_seconds=0)

    async def changed_during_read(_):
        await db.agents.rename("ike", "renamed")
        await db.agents.upsert(
            "ike",
            dir="/tmp/fixture-replacement",
            runtime="codex",
            model=None,
            repo="",
            tags=[],
            env={},
            description="",
            unattended=False,
        )
        return [EnrichedAgent(name="ike", session="ike", state="busy")]

    try:
        with patch(
            "agent_backbone.api.session_updates.build_session_snapshot",
            side_effect=changed_during_read,
        ):
            await feed.snapshot()
        current = await identity(db, "ike")
        async with db.engine.begin() as conn:
            assert (
                await conn.scalar(text("SELECT COUNT(*) FROM registered_state_observations")) == 0
            )
        config = replace(config, agents=agents_from_rows(await db.agents.list()))
        with patch(
            "agent_backbone.api.session_updates.build_session_snapshot",
            AsyncMock(
                return_value=[
                    EnrichedAgent(name="ike", session="ike", state="idle", evidence=["fixture"])
                ]
            ),
        ):
            await feed.snapshot()
        async with db.engine.begin() as conn:
            ids = (
                (await conn.execute(text("SELECT agent_id FROM registered_state_observations")))
                .scalars()
                .all()
            )
        assert ids == [current] and current != old
    finally:
        await feed.close()


async def test_registered_samples_are_not_kept_with_diagnostics_off(api_app):
    db = api_app.state.db
    await db.settings.set("diagnostics.mode", "off")
    await sample(db, "ike", await identity(db, "ike"), 0)
    async with db.engine.begin() as conn:
        count = await conn.scalar(text("SELECT COUNT(*) FROM registered_state_observations"))
    assert count == 0


async def test_a_window_at_the_end_of_time_is_answered(api_app):
    db = api_app.state.db
    end = "9999-12-31T23:59:59.000000Z"
    summary = await db.interactions.summary(since="9999-12-31T23:00:00.000000Z", until=end)
    assert summary["coverage"]["until"] == end
    spans = await db.interactions.state_spans(
        agent_id=await identity(db, "ike"), since="9999-12-31T23:00:00.000000Z", until=end
    )
    assert spans["items"] == []


def test_window_bounds_before_year_1000_stay_four_digit():
    from datetime import UTC, datetime

    from agent_backbone.services.database._time import format_iso, parse_iso

    early = format_iso(datetime(5, 1, 1, tzinfo=UTC))
    assert early == "0005-01-01T00:00:00.000000Z" and parse_iso(early).year == 5
