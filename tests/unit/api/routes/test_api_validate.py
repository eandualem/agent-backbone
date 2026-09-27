"""``backbone message validate``: the caller, the grant, reset and failure bounds."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_backbone import signing
from agent_backbone.api import validation
from agent_backbone.config import AgentsConfig
from agent_backbone.services.terminal import CallerUnknown
from tests.unit.api.routes.test_api_confirmations import TEXT, _body, _confirmation, _send
from tests.unit.api.routes.test_api_signing import (
    NOW,
    SENDER,
    _digest,
    _enroll,
    _pub,
    _transition,
)

DAY = 86400


@pytest.fixture(autouse=True)
def _signing_clock(monkeypatch):
    monkeypatch.setattr("agent_backbone.api.signed.now", lambda: NOW)
    monkeypatch.setattr("agent_backbone.api.routes.signing.now", lambda: NOW)


@pytest.fixture
def drain(monkeypatch):
    """The immediate delivery attempt; tests record deliveries themselves."""
    monkeypatch.setattr("agent_backbone.api.routes.messages.deliver_now", AsyncMock())


@pytest.fixture
async def key(api_client, auth_headers, api_app):
    private = Ed25519PrivateKey.generate()
    await _enroll(api_client, auth_headers, api_app.state.db, private)
    return private


@pytest.fixture
def caller(monkeypatch):
    """Where the calling process runs: the sessions of its pane, per process."""
    where = SimpleNamespace(sessions=[{"ike"}])

    async def sessions(client, server):
        if isinstance(where.sessions, Exception):
            raise where.sessions
        return where.sessions

    monkeypatch.setattr(validation, "caller_sessions", sessions)
    return where


@pytest.fixture
def clock(monkeypatch):
    now = SimpleNamespace(wall=time.time(), mono=1000.0)
    fake = SimpleNamespace(time=lambda: now.wall, monotonic=lambda: now.mono)
    monkeypatch.setattr(validation, "time", fake)
    return now


@pytest.fixture
def notices(monkeypatch):
    monkeypatch.setattr(validation, "NOTICE_SECONDS", 0.05)
    notify = AsyncMock(return_value=True)
    monkeypatch.setattr(validation, "notify_humans", notify)
    return notify


async def _admitted(client, headers, db, key, text=TEXT) -> dict:
    confirmation = _confirmation(text)
    resp, _ = await _send(client, headers, db, key, _body(confirmation, text))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _delivered(client, headers, db, key, text=TEXT) -> str:
    admitted = await _admitted(client, headers, db, key, text)
    await db.deliveries.record(
        issue_number=None,
        target_entity="ike",
        session_name="ike",
        outcome="delivered",
        source="delivery-retry-queue",
        kind="direct_message",
        preview="x",
        operation_id=admitted["operation_id"],
    )
    return admitted["confirmation_id"]


async def _validate(client, headers, confirmation_id: str, *, done: bool = False):
    return await client.post(
        "/api/messages/validate",
        headers=headers,
        json={"confirmation_id": confirmation_id, "done": done},
    )


def _reason(resp) -> str:
    return resp.json()["detail"]["reason"]


async def test_the_recipient_claims_the_exact_text_recovers_it_and_closes_it(
    api_client, auth_headers, api_app, key, drain, caller, clock
):
    cid = await _delivered(api_client, auth_headers, api_app.state.db, key)
    first = await _validate(api_client, auth_headers, cid)
    assert first.status_code == 200, first.text
    claim = first.json()
    assert claim["outcome"] == "claimed" and claim["recovered"] is False
    assert claim["text"] == TEXT and claim["sender"] == SENDER and claim["agent"] == "ike"
    assert claim["grant"]["expires_at"] == claim["grant"]["claimed_at"] + DAY

    again = (await _validate(api_client, auth_headers, cid)).json()
    assert again["outcome"] == "recovered" and again["recovered"] is True
    assert again["text"] == TEXT and again["grant"] == claim["grant"]

    closed = await _validate(api_client, auth_headers, cid, done=True)
    assert closed.json()["outcome"] == "closed" and "text" not in closed.json()
    # A lost response retried closes nothing twice; a claim is over for good.
    assert (await _validate(api_client, auth_headers, cid, done=True)).status_code == 200
    after = await _validate(api_client, auth_headers, cid)
    assert after.status_code == 410 and _reason(after) == "grant_closed"


async def test_another_agent_gets_no_text(
    api_client, auth_headers, api_app, key, drain, caller, clock
):
    cid = await _delivered(api_client, auth_headers, api_app.state.db, key)
    caller.sessions = [{"leo"}]
    resp = await _validate(api_client, auth_headers, cid)
    assert resp.status_code == 403 and _reason(resp) == "wrong_recipient"
    assert TEXT.strip() not in resp.text


@pytest.mark.parametrize(
    "where",
    [
        CallerUnknown("the calling process doesn't run in a tmux pane"),
        [{"someone-elses-shell"}],  # a pane, but no registered agent's
        [{"ike", "leo"}],  # one pane shown by two agents' sessions
        [{"ike"}, {"leo"}],  # processes of two agents share the connection
    ],
)
async def test_a_caller_that_isnt_one_agent_is_refused(
    api_client, auth_headers, api_app, key, drain, caller, clock, where
):
    cid = await _delivered(api_client, auth_headers, api_app.state.db, key)
    caller.sessions = where
    resp = await _validate(api_client, auth_headers, cid)
    assert resp.status_code == 403 and _reason(resp) == "caller_unidentified"
    assert TEXT.strip() not in resp.text


async def test_a_fabricated_id_is_refused(api_client, auth_headers, caller, clock):
    resp = await _validate(api_client, auth_headers, str(uuid.uuid4()))
    assert resp.status_code == 404 and _reason(resp) == "unknown_confirmation"


async def test_it_counts_once_it_reached_the_agent_including_through_its_inbox(
    api_client, auth_headers, api_app, key, drain, caller, clock
):
    admitted = await _admitted(api_client, auth_headers, api_app.state.db, key)
    cid = admitted["confirmation_id"]
    resp = await _validate(api_client, auth_headers, cid)
    assert resp.status_code == 409 and _reason(resp) == "not_delivered"
    read = await api_client.post(
        "/api/messages/inbox", headers=auth_headers, json={"session": "ike"}
    )
    assert [m["operation_id"] for m in read.json()["messages"]] == [admitted["operation_id"]]
    # Read, not yet acknowledged: the agent validates before acting on it.
    assert (await _validate(api_client, auth_headers, cid)).json()["outcome"] == "claimed"


async def test_the_claim_window_and_the_grant_both_last_a_day(
    api_client, auth_headers, api_app, key, drain, caller, clock
):
    db = api_app.state.db
    late = await _delivered(api_client, auth_headers, db, key)
    held = await _delivered(api_client, auth_headers, db, key, "Another step\n")
    assert (await _validate(api_client, auth_headers, held)).json()["outcome"] == "claimed"
    clock.wall += DAY + 1
    resp = await _validate(api_client, auth_headers, late)
    assert resp.status_code == 410 and _reason(resp) == "claim_window_passed"
    resp = await _validate(api_client, auth_headers, held)
    assert resp.status_code == 410 and _reason(resp) == "grant_expired"


async def test_a_claim_holds_only_in_the_directory_it_was_made_from(
    api_client, auth_headers, api_app, key, drain, caller, clock, tmp_path
):
    cid = await _delivered(api_client, auth_headers, api_app.state.db, key)
    assert (await _validate(api_client, auth_headers, cid)).status_code == 200
    config = api_app.state.config
    specs = {spec.name: spec for spec in config.agents}
    specs["ike"] = replace(specs["ike"], dir=str(tmp_path / "elsewhere"))
    api_app.state.config = replace(config, agents=AgentsConfig(specs=specs))
    resp = await _validate(api_client, auth_headers, cid)
    assert resp.status_code == 403 and _reason(resp) == "wrong_workspace"


async def test_a_reset_ends_its_authority_and_a_rotation_does_not(
    api_client, auth_headers, api_app, key, drain, caller, clock
):
    db = api_app.state.db
    claimed = await _delivered(api_client, auth_headers, db, key)
    unclaimed = await _delivered(api_client, auth_headers, db, key, "Another step\n")
    assert (await _validate(api_client, auth_headers, claimed)).status_code == 200

    rotated = Ed25519PrivateKey.generate()
    raw = signing.public_key(_pub(rotated))
    assert await db.signing.rotate(
        sender_key=signing.same_name(SENDER),
        old_epoch=1,
        new_public_key=_pub(rotated),
        new_fingerprint=signing.fingerprint(raw),
    )
    assert (await _validate(api_client, auth_headers, claimed)).json()["outcome"] == "recovered"

    view = await _transition(
        api_client, auth_headers, db, Ed25519PrivateKey.generate(), "replace", 2
    )
    assert (await db.signing.apply_transition(_digest(view.json()), now=NOW, by="t"))[0] == (
        "applied"
    )
    for cid in (claimed, unclaimed):
        resp = await _validate(api_client, auth_headers, cid)
        assert resp.status_code == 403 and _reason(resp) == "revoked"
        assert "Another step" not in resp.text and TEXT.strip() not in resp.text


async def test_failures_are_bounded_cached_and_noticed_once_per_incident(
    api_client, auth_headers, caller, clock, notices
):
    fabricated = [str(uuid.uuid4()) for _ in range(validation.MAX_FAILURES)]
    for cid in fabricated:
        assert _reason(await _validate(api_client, auth_headers, cid)) == "unknown_confirmation"
    # Bounded: no further checks this incident; a known refusal is repeated.
    fresh = await _validate(api_client, auth_headers, str(uuid.uuid4()))
    assert fresh.status_code == 429 and _reason(fresh) == "rate_limited"
    repeat = await _validate(api_client, auth_headers, fabricated[0])
    assert _reason(repeat) == "unknown_confirmation"

    await asyncio.sleep(0.1)
    notices.assert_awaited_once()
    text = notices.await_args.args[1]
    assert "12 failed validations" in text and "agent 'ike'" in text
    assert "rate_limited ×1" in text and "unknown_confirmation ×11" in text

    clock.mono += validation.INCIDENT_SECONDS  # quiet long enough: a new incident
    resp = await _validate(api_client, auth_headers, str(uuid.uuid4()))
    assert _reason(resp) == "unknown_confirmation"
    await asyncio.sleep(0.1)
    assert notices.await_count == 2
