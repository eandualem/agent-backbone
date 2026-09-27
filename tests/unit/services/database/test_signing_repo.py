"""The enrollment record: changed only by an approved transition or a signed rotation."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from agent_backbone import signing

NOW = 1_790_496_000


async def _start(db, action, expected, pub, *, request_id, key="assistant"):
    audience = await db.signing.audience()
    fp = signing.fingerprint(signing.public_key(pub)) if pub else "none"
    digest = signing.transition_digest(
        action=action,
        sender=key,
        audience=audience,
        expected_epoch=expected,
        new_fingerprint=fp,
        request_id=request_id,
        expires_at=NOW + 900,
    )
    await db.signing.start_transition(
        sender=key,
        sender_key=signing.same_name(key),
        action=action,
        audience=audience,
        expected_epoch=expected,
        new_public_key=pub,
        new_fingerprint=fp,
        request_id=request_id,
        digest=digest,
        expires_at=NOW + 900,
    )
    return digest


PUB_A = signing.b64url_encode(b"a" * 32)
PUB_B = signing.b64url_encode(b"b" * 32)


async def test_audience_is_created_once(db):
    assert await db.signing.audience() == await db.signing.audience()


async def test_set_replace_clear_through_approved_transitions(db):
    digest = await _start(db, "set", 0, PUB_A, request_id="r1")
    assert await db.signing.enrollment("assistant") is None  # pending is not enrolled
    assert (await db.signing.watched(NOW))["assistant"]["pending"]["digest"] == digest
    assert (await db.signing.apply_transition(digest, now=NOW, by="telegram:1"))[0] == "applied"
    assert (await db.signing.enrollment("assistant"))["epoch"] == 1
    assert (await db.signing.apply_transition(digest, now=NOW, by="telegram:1"))[0] == "not_pending"

    stale = await _start(db, "replace", 0, PUB_B, request_id="r2")  # expects the wrong epoch
    assert (await db.signing.apply_transition(stale, now=NOW, by="telegram:1"))[
        0
    ] == "epoch_changed"
    replace = await _start(db, "replace", 1, PUB_B, request_id="r3")
    assert (await db.signing.apply_transition(replace, now=NOW, by="telegram:1"))[0] == "applied"
    assert (await db.signing.enrollment("assistant"))["epoch"] == 2

    clear = await _start(db, "clear", 2, None, request_id="r4")
    assert (await db.signing.apply_transition(clear, now=NOW, by="telegram:1"))[0] == "applied"
    assert await db.signing.enrollment("assistant") is None  # the name is released


async def test_expired_superseded_and_unknown_transitions_change_nothing(db):
    old = await _start(db, "set", 0, PUB_A, request_id="r1")
    newer = await _start(db, "set", 0, PUB_B, request_id="r2")
    assert (await db.signing.apply_transition(old, now=NOW, by="x"))[0] == "not_pending"
    assert (await db.signing.apply_transition(newer, now=NOW + 901, by="x"))[0] == "expired"
    assert (await db.signing.apply_transition("0" * 64, now=NOW, by="x"))[0] == "unknown"
    assert await db.signing.enrollment("assistant") is None


async def test_rotation_moves_the_epoch_once(db):
    await db.signing.apply_transition(
        await _start(db, "set", 0, PUB_A, request_id="r1"), now=NOW, by="x"
    )
    fp = signing.fingerprint(b"b" * 32)
    rotated = await db.signing.rotate(
        sender_key="assistant", old_epoch=1, new_public_key=PUB_B, new_fingerprint=fp
    )
    assert rotated["epoch"] == 2 and rotated["public_key"] == PUB_B
    assert (
        await db.signing.rotate(
            sender_key="assistant", old_epoch=1, new_public_key=PUB_A, new_fingerprint=fp
        )
        is None
    )


async def test_nonces_detect_identical_retries_and_reuse(db):
    kw = {"sender_key": "assistant", "epoch": 1, "nonce": "0" * 32, "now": NOW}
    assert await db.signing.use_nonce(request_hash="h1", **kw) == ("new", None)
    assert (await db.signing.use_nonce(request_hash="h1", **kw))[0] == "replay"
    assert (await db.signing.use_nonce(request_hash="h2", **kw))[0] == "reused"


async def test_owner_is_set_once_then_changed_only_by_the_current_owner(db):
    assert await db.signing.set_owner(111) == "set"
    assert await db.signing.set_owner(222) == "pending"
    assert (await db.signing.owner())["telegram_user_id"] == 111
    assert not await db.signing.approve_owner_change(by_user_id=222, new_user_id=222)
    assert await db.signing.approve_owner_change(by_user_id=111, new_user_id=222)
    assert (await db.signing.owner()) == {"telegram_user_id": 222, "pending_user_id": None}


async def test_a_name_enrolled_again_after_a_clear_takes_a_new_epoch(db):
    await db.signing.apply_transition(
        await _start(db, "set", 0, PUB_A, request_id="r1"), now=NOW, by="x"
    )
    await db.signing.apply_transition(
        await _start(db, "clear", 1, None, request_id="r2"), now=NOW, by="x"
    )
    again = await _start(db, "set", 0, PUB_B, request_id="r3")
    assert (await db.signing.apply_transition(again, now=NOW, by="x"))[0] == "applied"
    assert (await db.signing.enrollment("assistant"))["epoch"] == 2  # epoch 1 stays retired


async def test_a_rotation_that_lands_first_makes_the_transition_stale(db):
    await db.signing.apply_transition(
        await _start(db, "set", 0, PUB_A, request_id="r1"), now=NOW, by="x"
    )
    clear = await _start(db, "clear", 1, None, request_id="r2")
    fp = signing.fingerprint(b"b" * 32)
    await db.signing.rotate(
        sender_key="assistant", old_epoch=1, new_public_key=PUB_B, new_fingerprint=fp
    )
    assert (await db.signing.apply_transition(clear, now=NOW, by="x"))[0] == "epoch_changed"
    kept = await db.signing.enrollment("assistant")
    assert kept["epoch"] == 2 and kept["public_key"] == PUB_B


async def test_at_most_one_transition_per_name_is_pending(db):
    await _start(db, "set", 0, PUB_A, request_id="r1")
    with pytest.raises(IntegrityError):  # what a racing second start would hit
        async with db.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO signing_transitions (request_id, sender, sender_key, action,"
                    " audience, expected_epoch, new_fingerprint, digest, created_at, expires_at,"
                    " status) VALUES ('r2', 'assistant', 'assistant', 'set', 'a', 0, 'f',"
                    " 'd2', 'now', 1, 'pending')"
                )
            )


async def test_prune_keeps_the_enrollment_history(db):
    await db.signing.apply_transition(
        await _start(db, "set", 0, PUB_A, request_id="r1"), now=NOW, by="x"
    )
    await db.signing.audit(kind="refusal", outcome="signature_required", sender_key="assistant")
    await db.signing.audit(kind="observation", outcome="unsigned", sender_key="assistant")
    assert await db.signing.prune(-1) == 2  # everything is older than tomorrow
    async with db.engine.begin() as conn:
        kinds = (await conn.execute(text("SELECT kind FROM signing_audit"))).scalars().all()
    assert kinds == ["transition"]


async def test_an_observation_cursor_never_sees_a_reused_seq(db):
    """Pruning the newest rows must not hand their seq to the next one."""
    last = await db.signing.audit(kind="observation", outcome="unsigned", sender_key="assistant")
    await db.signing.prune(-1)
    after = await db.signing.audit(kind="observation", outcome="unsigned", sender_key="assistant")
    assert after > last
