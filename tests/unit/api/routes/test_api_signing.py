"""Signed senders at the API: enrollment, enforcement, observation and rotation."""

from __future__ import annotations

import json
import secrets
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import text

from agent_backbone import signing
from agent_backbone.models import DeliveryOutcome
from agent_backbone.services.routing import DeliveryReport

NOW = 1_790_496_000
SENDER = "assistant"


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    monkeypatch.setattr("agent_backbone.api.signed.now", lambda: NOW)
    monkeypatch.setattr("agent_backbone.api.routes.signing.now", lambda: NOW)


@pytest.fixture
def deliver():
    mock = AsyncMock(return_value=DeliveryReport(DeliveryOutcome.DELIVERED))
    with patch("agent_backbone.api.routes.messages.safe_deliver", mock):
        yield mock


def _pub(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return signing.b64url_encode(raw)


async def _transition(client, headers, db, key, action="set", expected=0):
    request_id = str(uuid.uuid4())
    audience = await db.signing.audience()
    pub = _pub(key) if key else None
    proof = None
    if key is not None:
        proof = signing.b64url_encode(
            key.sign(
                signing.enroll_proof_bytes(
                    sender=SENDER,
                    audience=audience,
                    action=action,
                    expected_epoch=expected,
                    new_public_key=pub,
                    request_id=request_id,
                )
            )
        )
    body = {
        "sender": SENDER,
        "action": action,
        "expected_epoch": expected,
        "new_public_key": pub,
        "request_id": request_id,
        "proof": proof,
    }
    return await client.post("/api/signing/transitions", headers=headers, json=body)


def _digest(view: dict) -> str:
    return signing.transition_digest(
        action=view["action"],
        sender=view["sender"],
        audience=view["audience"],
        expected_epoch=view["expected_epoch"],
        new_fingerprint=view["new_fingerprint"],
        request_id=view["request_id"],
        expires_at=view["expires_at"],
    )


async def _enroll(client, headers, db, key) -> None:
    resp = await _transition(client, headers, db, key)
    assert resp.status_code == 200, resp.text
    assert (await db.signing.apply_transition(_digest(resp.json()), now=NOW, by="t"))[
        0
    ] == "applied"


async def _signed(db, key, method, path, body: dict | None, *, epoch=1, sender=SENDER, **over):
    raw = json.dumps(body).encode() if body is not None else b""
    fields = {
        "timestamp": NOW,
        "nonce": secrets.token_hex(16),
        "audience": await db.signing.audience(),
        "purpose": "request",
        **over,
    }
    framed = signing.request_bytes(
        purpose=fields["purpose"],
        method=method,
        path=path,
        query="",
        body=raw,
        timestamp=fields["timestamp"],
        nonce=fields["nonce"],
        audience=fields["audience"],
        sender=sender,
        epoch=epoch,
    )
    return raw, {
        "X-Backbone-Sender": sender,
        "X-Backbone-Key-Epoch": str(epoch),
        "X-Backbone-Timestamp": str(fields["timestamp"]),
        "X-Backbone-Nonce": fields["nonce"],
        "X-Backbone-Audience": fields["audience"],
        "X-Backbone-Signature": signing.b64url_encode(key.sign(framed)),
        "Content-Type": "application/json",
    }


MESSAGE = {"target_session": "ike", "from_entity": SENDER, "message": "hi\n"}


async def test_before_enrollment_nothing_changes(api_client, auth_headers, api_app, deliver):
    key = Ed25519PrivateKey.generate()
    raw, signed = await _signed(api_app.state.db, key, "POST", "/api/messages", MESSAGE)
    resp = await api_client.post("/api/messages", headers={**auth_headers, **signed}, content=raw)
    assert resp.status_code == 200  # headers under a name with no key are ignored
    resp = await api_client.post("/api/messages", headers=auth_headers, json=MESSAGE)
    assert resp.status_code == 200
    assert deliver.await_count == 2


async def test_an_enrolled_name_needs_a_valid_signature(api_client, auth_headers, api_app, deliver):
    db = api_app.state.db
    key = Ed25519PrivateKey.generate()
    await _enroll(api_client, auth_headers, db, key)

    for sender in (SENDER, "ASSISTANT", "ａｓｓｉｓｔａｎｔ"):
        resp = await api_client.post(
            "/api/messages", headers=auth_headers, json={**MESSAGE, "from_entity": sender}
        )
        assert resp.status_code == 403, sender
        assert resp.json()["detail"]["reason"] == "signature_required"
    deliver.assert_not_awaited()

    raw, signed = await _signed(db, key, "POST", "/api/messages", MESSAGE)
    resp = await api_client.post("/api/messages", headers={**auth_headers, **signed}, content=raw)
    assert resp.status_code == 200, resp.text
    assert deliver.await_count == 1

    again = await api_client.post("/api/messages", headers={**auth_headers, **signed}, content=raw)
    assert again.status_code == 409 and again.json()["detail"]["reason"] == "nonce_reused"

    # Other senders, and requests where the name is only the subject, are untouched.
    resp = await api_client.post(
        "/api/messages", headers=auth_headers, json={**MESSAGE, "from_entity": "peer"}
    )
    assert resp.status_code == 200
    resp = await api_client.get(f"/api/reports?agent={SENDER}", headers=auth_headers)
    assert resp.status_code != 403


@pytest.mark.parametrize(
    ("change", "reason", "status"),
    [
        ({"body": {**MESSAGE, "message": "changed"}}, "signature_invalid", 403),
        ({"over": {"audience": "0b6c1f2e-3a4d-4e5f-8a9b-0c1d2e3f4a5c"}}, "audience_mismatch", 403),
        ({"over": {"timestamp": NOW - 301}}, "timestamp_out_of_window", 403),
        ({"epoch": 2}, "key_epoch_unknown", 403),
        ({"over": {"purpose": "rotate"}}, "signature_invalid", 403),
        ({"sender": "peer"}, "sender_mismatch", 403),
    ],
)
async def test_a_bad_signature_is_refused_and_audited(
    api_client, auth_headers, api_app, deliver, change, reason, status
):
    db = api_app.state.db
    key = Ed25519PrivateKey.generate()
    await _enroll(api_client, auth_headers, db, key)
    raw, signed = await _signed(
        db,
        key,
        "POST",
        "/api/messages",
        MESSAGE,
        epoch=change.get("epoch", 1),
        sender=change.get("sender", SENDER),
        **change.get("over", {}),
    )
    if "body" in change:
        raw = json.dumps(change["body"]).encode()
    resp = await api_client.post("/api/messages", headers={**auth_headers, **signed}, content=raw)
    assert resp.status_code == status, resp.text
    assert resp.json()["detail"]["reason"] == reason
    deliver.assert_not_awaited()
    async with db.engine.begin() as conn:
        rows = (
            await conn.execute(text("SELECT * FROM signing_audit WHERE kind = 'refusal'"))
        ).fetchall()
    assert [(r.outcome, r.target, r.path) for r in rows] == [(reason, "ike", "/api/messages")]
    assert "hi" not in " ".join(str(v) for v in rows[0])  # metadata only, never the body


async def test_a_duplicate_key_is_refused_once_a_name_is_watched(
    api_client, auth_headers, api_app, deliver
):
    await _enroll(api_client, auth_headers, api_app.state.db, Ed25519PrivateKey.generate())
    body = '{"target_session":"ike","from_entity":"peer","from_entity":"assistant","message":"x"}'
    resp = await api_client.post(
        "/api/messages", headers={**auth_headers, "Content-Type": "application/json"}, content=body
    )
    assert resp.status_code == 422 and resp.json()["detail"]["reason"] == "malformed_request"
    deliver.assert_not_awaited()


async def test_a_pending_key_is_observed_without_changing_admission(
    api_client, auth_headers, api_app, deliver
):
    db = api_app.state.db
    key = Ed25519PrivateKey.generate()
    resp = await _transition(api_client, auth_headers, db, key)
    assert resp.status_code == 200
    state = (
        await api_client.get(f"/api/signing/enrollment?sender={SENDER}", headers=auth_headers)
    ).json()
    assert state["epoch"] == 0 and state["pending"]["action"] == "set"
    assert "digest" not in state["pending"]

    raw, signed = await _signed(db, key, "POST", "/api/messages", MESSAGE, epoch=1)
    ok = await api_client.post("/api/messages", headers={**auth_headers, **signed}, content=raw)
    plain = await api_client.post("/api/messages", headers=auth_headers, json=MESSAGE)
    assert ok.status_code == plain.status_code == 200
    seen = await api_client.get(f"/api/signing/observations?sender={SENDER}", headers=auth_headers)
    assert [o["outcome"] for o in seen.json()["observations"]] == ["verified", "unsigned"]


async def test_transition_refusals(api_client, auth_headers, api_app):
    db = api_app.state.db
    key = Ed25519PrivateKey.generate()
    resp = await _transition(api_client, auth_headers, db, key, expected=3)
    assert resp.json()["detail"]["reason"] == "epoch_changed"
    resp = await _transition(api_client, auth_headers, db, None, action="replace", expected=0)
    assert resp.json()["detail"]["reason"] == "not_enrolled"
    await _enroll(api_client, auth_headers, db, key)
    resp = await _transition(api_client, auth_headers, db, key)
    assert resp.json()["detail"]["reason"] == "already_enrolled"
    # A proof by another key than the one named is refused.
    other = Ed25519PrivateKey.generate()
    request_id = str(uuid.uuid4())
    proof = other.sign(
        signing.enroll_proof_bytes(
            sender=SENDER,
            audience=await db.signing.audience(),
            action="replace",
            expected_epoch=1,
            new_public_key=_pub(key),
            request_id=request_id,
        )
    )
    resp = await api_client.post(
        "/api/signing/transitions",
        headers=auth_headers,
        json={
            "sender": SENDER,
            "action": "replace",
            "expected_epoch": 1,
            "new_public_key": _pub(key),
            "request_id": request_id,
            "proof": signing.b64url_encode(proof),
        },
    )
    assert resp.status_code == 403 and resp.json()["detail"]["reason"] == "signature_invalid"


async def test_rotation_signed_by_the_current_key(api_client, auth_headers, api_app, deliver):
    db = api_app.state.db
    old, new = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    await _enroll(api_client, auth_headers, db, old)
    nonce = secrets.token_hex(16)
    proof = new.sign(
        signing.rotate_proof_bytes(
            sender=SENDER,
            audience=await db.signing.audience(),
            old_epoch=1,
            new_public_key=_pub(new),
            nonce=nonce,
            timestamp=NOW,
        )
    )
    body = {"new_public_key": _pub(new), "proof": signing.b64url_encode(proof)}
    raw, signed = await _signed(
        db, old, "POST", "/api/signing/rotation", body, purpose="rotate", nonce=nonce
    )
    resp = await api_client.post(
        "/api/signing/rotation", headers={**auth_headers, **signed}, content=raw
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["epoch"] == 2

    raw, signed = await _signed(db, old, "POST", "/api/messages", MESSAGE, epoch=2)
    resp = await api_client.post("/api/messages", headers={**auth_headers, **signed}, content=raw)
    assert resp.json()["detail"]["reason"] == "signature_invalid"  # the old key no longer admits
    raw, signed = await _signed(db, new, "POST", "/api/messages", MESSAGE, epoch=2)
    resp = await api_client.post("/api/messages", headers={**auth_headers, **signed}, content=raw)
    assert resp.status_code == 200


async def test_unsigned_rotation_is_refused(api_client, auth_headers):
    resp = await api_client.post(
        "/api/signing/rotation",
        headers=auth_headers,
        json={"new_public_key": "x", "proof": "y"},
    )
    assert resp.status_code == 403


async def test_owner_is_set_once(api_client, auth_headers):
    first = await api_client.post(
        "/api/signing/owner", headers=auth_headers, json={"telegram_user_id": 11}
    )
    second = await api_client.post(
        "/api/signing/owner", headers=auth_headers, json={"telegram_user_id": 22}
    )
    assert first.json() == {"outcome": "set"} and second.json() == {"outcome": "pending"}


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32", "utf-8-sig"])
async def test_a_body_the_check_cant_read_is_refused(
    api_client, auth_headers, api_app, deliver, encoding
):
    """The API itself decodes these; the check must not let them through unsigned."""
    await _enroll(api_client, auth_headers, api_app.state.db, Ed25519PrivateKey.generate())
    resp = await api_client.post(
        "/api/messages",
        headers={**auth_headers, "Content-Type": "application/json"},
        content=json.dumps(MESSAGE).encode(encoding),
    )
    assert resp.status_code in (403, 422), resp.text
    deliver.assert_not_awaited()


async def test_a_pending_replace_is_observed_while_the_active_key_admits(
    api_client, auth_headers, api_app, deliver
):
    db = api_app.state.db
    old, new = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    await _enroll(api_client, auth_headers, db, old)
    resp = await _transition(api_client, auth_headers, db, new, action="replace", expected=1)
    assert resp.status_code == 200 and resp.json()["new_epoch"] == 2

    raw, signed = await _signed(db, new, "POST", "/api/messages", MESSAGE, epoch=2)
    refused = await api_client.post(
        "/api/messages", headers={**auth_headers, **signed}, content=raw
    )
    assert refused.json()["detail"]["reason"] == "key_epoch_unknown"  # not admitted yet
    raw, signed = await _signed(db, old, "POST", "/api/messages", MESSAGE, epoch=1)
    admitted = await api_client.post(
        "/api/messages", headers={**auth_headers, **signed}, content=raw
    )
    assert admitted.status_code == 200
    seen = await api_client.get(f"/api/signing/observations?sender={SENDER}", headers=auth_headers)
    assert [o["outcome"] for o in seen.json()["observations"]] == [
        "verified",
        "key_epoch_unknown",
    ]


async def test_a_signing_name_is_printable_ascii(api_client, auth_headers):
    """It travels in a header; a name that can't would lock itself out."""
    resp = await api_client.post(
        "/api/signing/transitions",
        headers=auth_headers,
        json={
            "sender": "助手",
            "action": "set",
            "expected_epoch": 0,
            "new_public_key": signing.b64url_encode(b"k" * 32),
            "request_id": str(uuid.uuid4()),
            "proof": "x",
        },
    )
    assert resp.status_code == 422


@pytest.mark.parametrize("sender", ["api", "unknown", "Backbone"])
async def test_a_name_the_api_fills_in_can_not_sign(api_client, auth_headers, sender):
    resp = await api_client.post(
        "/api/signing/transitions",
        headers=auth_headers,
        json={
            "sender": sender,
            "action": "set",
            "expected_epoch": 0,
            "new_public_key": signing.b64url_encode(b"k" * 32),
            "request_id": str(uuid.uuid4()),
            "proof": "x",
        },
    )
    assert resp.status_code == 422


async def test_enforced_when_the_app_is_mounted_under_a_prefix(api_app, auth_headers, deliver):
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=api_app, root_path="/bb")
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _enroll(client, auth_headers, api_app.state.db, Ed25519PrivateKey.generate())
        resp = await client.post("/bb/api/messages", headers=auth_headers, json=MESSAGE)
    assert resp.status_code == 403
    deliver.assert_not_awaited()


async def test_a_signed_rotation_with_a_duplicate_key_is_refused(api_client, auth_headers, api_app):
    db = api_app.state.db
    old = Ed25519PrivateKey.generate()
    await _enroll(api_client, auth_headers, db, old)
    key = signing.b64url_encode(b"k" * 32)
    body = f'{{"new_public_key":"{key}","new_public_key":"{key}","proof":"x"}}'.encode()
    _, signed = await _signed(db, old, "POST", "/api/signing/rotation", None, purpose="rotate")
    resp = await api_client.post(
        "/api/signing/rotation", headers={**auth_headers, **signed}, content=body
    )
    assert resp.status_code == 422 and resp.json()["detail"]["reason"] == "malformed_request"
    assert (await db.signing.enrollment(SENDER))["epoch"] == 1


@pytest.mark.parametrize("key", ["\\ud800", "k" * 7000])
async def test_a_refused_body_is_never_echoed(api_client, auth_headers, api_app, deliver, key):
    await _enroll(api_client, auth_headers, api_app.state.db, Ed25519PrivateKey.generate())
    body = f'{{"from_entity":"assistant","{key}":1,"{key}":2}}'
    resp = await api_client.post(
        "/api/messages", headers={**auth_headers, "Content-Type": "application/json"}, content=body
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == {
        "reason": "malformed_request",
        "message": "the body repeats a key",
    }


async def test_a_path_the_router_accepts_is_checked(api_client, auth_headers, api_app, deliver):
    """The router's pattern also takes a trailing newline; so must the check."""
    await _enroll(api_client, auth_headers, api_app.state.db, Ed25519PrivateKey.generate())
    resp = await api_client.post("/api/messages%0A", headers=auth_headers, json=MESSAGE)
    assert resp.status_code == 403 and resp.json()["detail"]["reason"] == "signature_required"
    deliver.assert_not_awaited()


def test_every_sender_route_is_a_real_route(api_app):
    from agent_backbone.api.signed import SENDER_ROUTES

    routes = {(m, r.path) for r in api_app.router.routes for m in getattr(r, "methods", None) or ()}
    assert {(r.method, r.template) for r in SENDER_ROUTES} <= routes


# Every shipped adapter, by name: the capability contract cites this test.
RUNTIME_IDS = ("claude", "codex", "gemini", "opencode", "deepcode", "aider", "shell")


def test_the_runtime_list_is_every_shipped_adapter():
    from agent_backbone.services.runtimes import RUNTIMES

    assert set(RUNTIME_IDS) == set(RUNTIMES)


@pytest.mark.parametrize("runtime", RUNTIME_IDS)
async def test_the_same_check_for_a_recipient_on_every_runtime(
    api_client, auth_headers, api_app, deliver, runtime
):
    """Enforcement happens at the API, before delivery: a recipient's runtime
    makes no difference to what is refused or admitted."""
    from dataclasses import replace

    from agent_backbone.config import AgentsConfig, AgentSpec

    config = api_app.state.config
    target = f"{runtime}-agent"
    specs = {**config.agents.specs, target: AgentSpec(name=target, dir="/tmp", runtime=runtime)}
    api_app.state.config = replace(config, agents=AgentsConfig(specs=specs))
    db = api_app.state.db
    key = Ed25519PrivateKey.generate()
    await _enroll(api_client, auth_headers, db, key)
    message = {**MESSAGE, "target_session": target}

    refused = await api_client.post("/api/messages", headers=auth_headers, json=message)
    assert refused.json()["detail"]["reason"] == "signature_required"
    raw, signed = await _signed(db, key, "POST", "/api/messages", message)
    admitted = await api_client.post(
        "/api/messages", headers={**auth_headers, **signed}, content=raw
    )
    assert admitted.status_code == 200, admitted.text
    assert deliver.await_args.kwargs["session_name"] == target


async def test_a_signed_message_that_waits_is_tracked_for_a_reset(
    api_client, auth_headers, api_app
):
    db = api_app.state.db
    key = Ed25519PrivateKey.generate()
    await _enroll(api_client, auth_headers, db, key)
    stored = DeliveryReport(DeliveryOutcome.AGENT_WORKING, "stored", queue_id=77)
    with patch("agent_backbone.api.routes.messages.safe_deliver", AsyncMock(return_value=stored)):
        raw, signed = await _signed(db, key, "POST", "/api/messages", MESSAGE)
        resp = await api_client.post(
            "/api/messages", headers={**auth_headers, **signed}, content=raw
        )
    assert resp.status_code == 200
    async with db.engine.begin() as conn:
        row = (await conn.execute(text("SELECT sender_key, epoch FROM signing_queued"))).one()
    assert tuple(row) == (SENDER, 1)


async def test_a_database_failure_is_not_a_reused_request_id(api_client, auth_headers, api_app):
    from sqlalchemy.exc import OperationalError

    failing = AsyncMock(side_effect=OperationalError("stmt", {}, Exception("locked")))
    with (
        patch.object(api_app.state.db.signing, "start_transition", failing),
        pytest.raises(OperationalError),
    ):
        await _transition(api_client, auth_headers, api_app.state.db, Ed25519PrivateKey.generate())


async def test_a_rotation_without_a_verifier_is_refused_in_shape(api_client, auth_headers, api_app):
    db = api_app.state.db
    old, new = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    await _enroll(api_client, auth_headers, db, old)
    body = {"new_public_key": _pub(new), "proof": signing.b64url_encode(b"x" * 64)}
    raw, signed = await _signed(db, old, "POST", "/api/signing/rotation", body, purpose="rotate")
    real = signing.verify
    calls = iter([real, None])

    def verify(*args):
        step = next(calls)
        if step is None:  # the route's proof check, after the middleware's
            raise signing.VerifierUnavailable("gone")
        return step(*args)

    with patch.object(signing, "verify", verify):
        resp = await api_client.post(
            "/api/signing/rotation", headers={**auth_headers, **signed}, content=raw
        )
    assert resp.status_code == 503 and resp.json()["detail"]["reason"] == "verifier_unavailable"
