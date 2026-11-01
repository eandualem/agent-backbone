"""Historical observations retain identity and never manufacture quiet or state."""

import json
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
    boundary = (await db.interactions.summary(since=at(0), until=at(3)))["coverage"]
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
    # The gap never moves where capture began.
    assert c["capture_started_at"] == boundary["capture_started_at"] is not None
    assert c["retained_from"] == boundary["retained_from"]
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


async def test_debug_evidence_preserves_causes_and_masks_known_credential_values(
    api_app, api_client, auth_headers
):
    db = api_app.state.db
    agent = await identity(db, "ike")
    await db.settings.set("diagnostics.mode", "debug")
    quoted = [
        "terminal shows a provider failure (codex): 401 Authorization: Bearer SYNTHETIC_SECRET_1",
        'terminal shows a provider failure (codex): api_key="SYNTHETIC_SECRET_2"',
        "terminal shows a provider failure (codex): password='SYNTHETIC_SECRET_3'",
        'terminal shows a provider failure (codex): {"api_key": "SYNTHETIC_SECRET_4"}',
        "hook state 'busy' written 0s ago (fresh, Bearer SYNTHETIC_SECRET_5)",
        "Authorization Basic SYNTHETIC_SECRET_6",
        "the failure on screen beats the hook's 'busy'",
        "no active permission dialog; the hook does not prove a human wait",
    ]
    await sample(db, "ike", agent, 0)
    await db.state_history.observe_registered(
        [("ike", "blocked", "provider")],
        registrations={"ike": agent},
        evidence={"ike": quoted},
        recorder_id="one",
    )
    async with db.engine.begin() as conn:
        stored = await conn.scalar(
            text("SELECT evidence FROM registered_state_observations ORDER BY id DESC LIMIT 1")
        )
        # A row written before this rule still holds raw text; reads never return it.
        await conn.execute(
            text("UPDATE registered_state_observations SET evidence=:raw"),
            {"raw": json.dumps(quoted)},
        )
    assert "SYNTHETIC_SECRET" not in stored
    saved = json.loads(stored)
    assert "(codex): 401 Authorization: Bearer [redacted]" in saved[0]
    assert "hook state 'busy' written 0s ago (fresh, Bearer [redacted])" == saved[4]
    assert saved[-2:] == quoted[-2:]
    spans = await api_client.get(
        "/api/interactions/state-spans",
        headers=auth_headers,
        params={"since": at(0), "until": at(5)},
    )
    assert spans.status_code == 200, spans.text
    assert "SYNTHETIC_SECRET" not in spans.text
    assert "terminal shows a provider failure" in spans.text


async def test_standard_omits_copied_evidence_and_off_stops_observations(api_app):
    db = api_app.state.db
    agent = await identity(db, "ike")
    await sample(db, "ike", agent, 0)
    async with db.engine.begin() as conn:
        assert await conn.scalar(text("SELECT evidence FROM registered_state_observations")) == "[]"
    await db.settings.set("diagnostics.mode", "off")
    await sample(db, "ike", agent, 1)
    await checkpoint(db, 1)
    async with db.engine.begin() as conn:
        assert await conn.scalar(text("SELECT COUNT(*) FROM registered_state_observations")) == 1
        assert (
            await conn.scalar(
                text(
                    "SELECT COUNT(*) FROM interaction_capture_checkpoints "
                    "WHERE COALESCE(reason,'') != 'diagnostics_mode_changed'"
                )
            )
            == 0
        )


async def test_short_off_interval_breaks_capture_even_without_an_off_poll(api_app):
    from dataclasses import replace
    from unittest.mock import AsyncMock

    from agent_backbone.api.session_updates import SessionFeed
    from agent_backbone.config import agents_from_rows
    from agent_backbone.services.agents import EnrichedAgent

    db = api_app.state.db
    agent = await identity(db, "ike")
    config = replace(api_app.state.config, agents=agents_from_rows(await db.agents.list()))
    feed = SessionFeed(lambda: config, db=db, ttl_seconds=0)
    with (
        patch(
            "agent_backbone.api.session_updates.build_session_snapshot",
            AsyncMock(return_value=[EnrichedAgent(name="ike", session="ike", state="busy")]),
        ),
        patch("agent_backbone.services.database._state_history_repo.now_iso") as state_time,
        patch(
            "agent_backbone.services.database._interaction_observations.now_iso"
        ) as checkpoint_time,
    ):
        state_time.return_value = checkpoint_time.return_value = at(0)
        await feed.snapshot()
        # The setting publisher notifies the feed even when no snapshot is requested.
        await db.settings.set("diagnostics.mode", "off")
        feed.set_diagnostic_mode("off")
        await db.settings.set("diagnostics.mode", "standard")
        feed.set_diagnostic_mode("standard")
        state_time.return_value = checkpoint_time.return_value = at(1)
        await feed.snapshot()
        state_time.return_value = checkpoint_time.return_value = at(2)
        await feed.snapshot()
    await feed.close()
    page = await db.interactions.state_spans(agent_id=agent, since=at(0), until=at(3))
    assert page["items"][0]["since"] == page["items"][0]["until"] == at(0)
    assert page["items"][1]["since"] == at(1) and page["items"][1]["until"] == at(2)
    coverage = (await db.interactions.summary(since=at(0), until=at(3)))["coverage"]
    assert coverage["message_intervals"] == [
        {"since": at(1), "until": at(2), "completeness": "complete_within_declared_scope"}
    ]
    assert "capture_interval_gaps" in coverage["reasons"]


@pytest.mark.parametrize("phase", ["snapshot", "history"])
async def test_mode_change_preserves_live_read_without_retaining_cross_boundary_sample(
    api_app, phase
):
    import asyncio
    from dataclasses import replace
    from unittest.mock import AsyncMock

    from agent_backbone.api.session_updates import SessionFeed
    from agent_backbone.config import agents_from_rows
    from agent_backbone.services.agents import EnrichedAgent

    db = api_app.state.db
    await identity(db, "ike")
    config = replace(api_app.state.config, agents=agents_from_rows(await db.agents.list()))
    feed = SessionFeed(lambda: config, db=db, ttl_seconds=0)
    entered, resume = asyncio.Event(), asyncio.Event()
    live = [EnrichedAgent(name="ike", session="ike", state="busy")]

    async def held(*args):
        entered.set()
        await resume.wait()
        return live

    target = (
        "agent_backbone.api.session_updates.build_session_snapshot"
        if phase == "snapshot"
        else "agent_backbone.api.session_updates.SessionFeed._record_history"
    )
    with (
        patch(
            "agent_backbone.api.session_updates.build_session_snapshot",
            AsyncMock(return_value=live),
        ),
        patch(target, side_effect=held),
    ):
        read = asyncio.create_task(feed.snapshot())
        await entered.wait()
        await db.settings.set("diagnostics.mode", "off")
        config = replace(config, settings={**config.settings, "diagnostics.mode": "off"})
        feed.set_diagnostic_mode("off")
        if phase == "snapshot":
            # Even a quick off/on interval invalidates the in-progress sample.
            await db.settings.set("diagnostics.mode", "standard")
            config = replace(config, settings={**config.settings, "diagnostics.mode": "standard"})
            feed.set_diagnostic_mode("standard")
        resume.set()
        assert await read == live
    await feed.close()
    async with db.engine.begin() as conn:
        assert await conn.scalar(text("SELECT COUNT(*) FROM registered_state_observations")) == 0
        assert (
            await conn.scalar(
                text(
                    "SELECT COUNT(*) FROM interaction_capture_checkpoints "
                    "WHERE COALESCE(reason,'') != 'diagnostics_mode_changed'"
                )
            )
            == 0
        )


@pytest.mark.parametrize("unset", [False, True])
async def test_missed_mode_publications_still_split_persisted_capture(api_app, unset):
    import asyncio
    from unittest.mock import AsyncMock

    db, store = api_app.state.db, api_app.state.agent_store
    agent = await identity(db, "ike")
    await sample(db, "ike", agent, 0)
    await checkpoint(db, 0)
    first_refresh = asyncio.Event()
    release = asyncio.Event()
    modes = []

    async def coalesced_refresh():
        first_refresh.set()
        await release.wait()
        modes.append((await db.settings.all()).get("diagnostics.mode", "standard"))
        return api_app.state.config

    with (
        patch.object(store, "refresh", AsyncMock(side_effect=coalesced_refresh)),
        patch("agent_backbone.services.database._settings_repo.now_iso", return_value=at(0, 20)),
    ):
        off = asyncio.create_task(store.set_setting("diagnostics.mode", "off"))
        await first_refresh.wait()
        # Both refreshes see only the last mode; no feed notification is made.
        on = asyncio.create_task(
            store.unset_setting("diagnostics.mode")
            if unset
            else store.set_setting("diagnostics.mode", "standard")
        )
        while (await db.settings.all()).get("diagnostics.mode", "standard") != "standard":
            await asyncio.sleep(0)
        release.set()
        await asyncio.gather(off, on)
    assert modes == ["standard", "standard"]
    await sample(db, "ike", agent, 1)
    await checkpoint(db, 1)
    await sample(db, "ike", agent, 2)
    await checkpoint(db, 2)
    spans = await db.interactions.state_spans(agent_id=agent, since=at(0), until=at(3))
    assert spans["items"][0]["since"] == spans["items"][0]["until"] == at(0)
    assert spans["items"][1]["since"] == at(1)
    coverage = (await db.interactions.summary(since=at(0), until=at(3)))["coverage"]
    assert coverage["message_intervals"] == [
        {"since": at(1), "until": at(2), "completeness": "complete_within_declared_scope"}
    ]


@pytest.mark.parametrize("surface", ["state", "checkpoint"])
async def test_policy_change_during_write_rejects_old_capture_generation(api_app, surface):
    from agent_backbone.services.database import _repo

    db = api_app.state.db
    agent = await identity(db, "ike")
    module = "_state_history_repo" if surface == "state" else "_interaction_observations"
    original = _repo.diagnostic_capture_policy

    async def change_after_read(conn):
        policy = await original(conn)
        # Same transaction simulates the persisted generation changing between
        # its initial read and conditional INSERT; it must not accept that sample.
        await conn.execute(
            text(
                "INSERT INTO interaction_capture_checkpoints "
                "(recorder_id,at,reason) VALUES "
                "('changed',:at,'diagnostics_mode_changed')"
            ),
            {"at": at(0)},
        )
        return policy

    with patch(
        f"agent_backbone.services.database.{module}.diagnostic_capture_policy", change_after_read
    ):
        if surface == "state":
            await sample(db, "ike", agent, 1)
        else:
            await checkpoint(db, 1)
    async with db.engine.begin() as conn:
        assert await conn.scalar(text("SELECT COUNT(*) FROM registered_state_observations")) == 0
        assert (
            await conn.scalar(
                text(
                    "SELECT COUNT(*) FROM interaction_capture_checkpoints "
                    "WHERE COALESCE(reason,'') != 'diagnostics_mode_changed'"
                )
            )
            == 0
        )


async def test_mode_change_does_not_interrupt_history_rollback(api_app):
    import asyncio

    from agent_backbone.api.session_updates import SessionFeed

    feed = SessionFeed(lambda: api_app.state.config, db=api_app.state.db)
    entered, rolling_back, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cleaned = []

    async def write():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            rolling_back.set()
            await release.wait()
            cleaned.append(True)

    task = asyncio.create_task(write())
    feed._history_task = task
    await entered.wait()
    task.cancel()
    await rolling_back.wait()
    feed.set_diagnostic_mode("off")
    assert task.cancelling() == 1
    release.set()
    await feed.close()
    assert cleaned == [True]


def test_limited_credential_forms_preserve_ordinary_basic_explanations():
    from agent_backbone.services.database._state_history_repo import stored_evidence

    lines = [
        "Running basic tests for module",
        "Authorization: token ghp_SYNTHETICSECRET8",
        "https://ghp_SYNTHETICSECRET9@github.com/org/repo",
        "Invalid API key sk-ant-SYNTHETIC_SECRET_7",
    ]
    saved = stored_evidence(lines)
    assert saved[0] == lines[0]
    assert "SYNTHETIC" not in "\n".join(saved)
    assert saved[2] == "https://[redacted]@github.com/org/repo"
    assert saved[3] == "Invalid API key [redacted]"


@pytest.mark.parametrize("surface", ["state", "checkpoint"])
async def test_separate_committed_policy_change_rejects_inflight_capture(tmp_path, surface):
    import asyncio

    from agent_backbone.services.database import BackboneDB, _repo

    db = BackboneDB(f"sqlite+aiosqlite:///{tmp_path / 'capture.db'}")
    await db.start()
    try:
        await db.agents.upsert(
            "worker",
            dir=str(tmp_path),
            runtime="shell",
            model=None,
            repo="",
            tags=[],
            env={},
            description="",
            unattended=False,
        )
        row = (await db.agents.list())[0]
        agent = row["report_identity"]
        assert agent
        ready, release = asyncio.Event(), asyncio.Event()
        original = _repo.diagnostic_capture_policy

        async def pause_after_read(conn):
            policy = await original(conn)
            ready.set()
            await release.wait()
            return policy

        module = "_state_history_repo" if surface == "state" else "_interaction_observations"
        with patch(
            f"agent_backbone.services.database.{module}.diagnostic_capture_policy", pause_after_read
        ):
            writing = asyncio.create_task(
                db.state_history.observe_registered(
                    [("worker", "busy", None)],
                    registrations={"worker": agent},
                    evidence={"worker": ["sample"]},
                    recorder_id="recorder",
                )
                if surface == "state"
                else db.interactions.checkpoint("recorder")
            )
            await asyncio.wait_for(ready.wait(), 3)
            await asyncio.wait_for(db.settings.set("diagnostics.mode", "off"), 3)
            release.set()
            await asyncio.wait_for(writing, 3)
        async with db.engine.begin() as conn:
            assert (
                await conn.scalar(text("SELECT COUNT(*) FROM registered_state_observations")) == 0
            )
            assert (
                await conn.scalar(
                    text(
                        "SELECT COUNT(*) FROM interaction_capture_checkpoints "
                        "WHERE COALESCE(reason,'') != 'diagnostics_mode_changed'"
                    )
                )
                == 0
            )
    finally:
        await db.stop()


async def test_latest_policy_boundary_survives_history_pruning(api_app):
    from agent_backbone.services.database._repo import diagnostic_capture_policy

    db = api_app.state.db
    with patch("agent_backbone.services.database._settings_repo.now_iso", return_value=at(0)):
        await db.settings.set("diagnostics.mode", "off")
        await db.settings.set("diagnostics.mode", "standard")
    async with db.engine.begin() as conn:
        before = await diagnostic_capture_policy(conn)
    await db.state_history.prune()
    async with db.engine.begin() as conn:
        assert await diagnostic_capture_policy(conn) == before
        assert await conn.scalar(text("SELECT COUNT(*) FROM interaction_capture_checkpoints")) == 1


async def test_snapshot_timeout_does_not_cancel_policy_rollback_twice(api_app):
    import asyncio
    from unittest.mock import AsyncMock

    from agent_backbone.api.session_updates import SessionFeed

    feed = SessionFeed(lambda: api_app.state.config, db=api_app.state.db)
    entered, rollback, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cleaned = []

    async def held(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            rollback.set()
            await release.wait()
            cleaned.append(True)

    with (
        patch(
            "agent_backbone.api.session_updates.build_session_snapshot", AsyncMock(return_value=[])
        ),
        patch.object(feed, "_record_history", side_effect=held),
        patch("agent_backbone.api.session_updates.HISTORY_WRITE_TIMEOUT_SECONDS", 0.05),
    ):
        reading = asyncio.create_task(feed.snapshot())
        await entered.wait()
        feed.set_diagnostic_mode("off")
        await rollback.wait()
        assert await reading == []
        assert feed._history_task.cancelling() == 1
        release.set()
        await feed.close()
    assert cleaned == [True]
