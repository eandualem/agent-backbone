"""A steer reaches the current turn through the hook, or is refused; never queued or pasted."""

from __future__ import annotations

import io
import json
import os
import time
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from agent_backbone.hooks import claude_hook, codex_hook
from agent_backbone.hooks.backbone_state import (
    offer_steer,
    retire_steers,
    steer_key,
    steer_offers,
    take_context,
)
from agent_backbone.services.agents import AgentState
from agent_backbone.services.routing import settle_steers, steer_agent
from agent_backbone.services.routing.models import SessionIntelligence, SessionProfile

_STEER = "agent_backbone.services.routing._steer"


def _profile(intel, runtime="claude"):
    return SessionProfile(
        "ike", intel, runtime=runtime, agent_state=AgentState.BUSY, evidence=["e"]
    )


@pytest.fixture
def working():
    with (
        patch(f"{_STEER}.get_session_intelligence", new_callable=AsyncMock) as intel,
        patch(f"{_STEER}.query_environment_var", new_callable=AsyncMock, return_value="L1") as env,
    ):
        intel.return_value = _profile(SessionIntelligence.AGENT_WORKING)
        yield intel, env


async def test_a_working_claude_agent_gets_the_offer_and_a_log_row(config, db, working):
    report = await steer_agent("ike", "check the lock first", config, db=db, sender="leo")
    assert report.outcome == "offered" and report.launch_id == "L1"
    assert report.delivery_id and report.operation_id
    rows = await db.deliveries.query(session_name="ike", kind="steer")
    assert [(r["outcome"], r["source"]) for r in rows] == [("offered", "api-steer")]
    assert rows[0]["preview"].startswith("[via:backbone from:leo] (steer for your current task) ")
    # Only that session takes it; nothing is in the queue.
    assert take_context(config.state_dir, "ike", launch_id="other") == []
    assert take_context(config.state_dir, "ike", launch_id="L1") == [
        "[via:backbone from:leo] (steer for your current task) check the lock first"
    ]
    assert await db.queue.sessions_with_pending() == []


@pytest.mark.parametrize(
    ("intel", "runtime", "reason"),
    [
        (SessionIntelligence.OFFLINE, "claude", "offline"),
        (SessionIntelligence.READY, "claude", "not_working"),
        (SessionIntelligence.WAITING_FOR_HUMAN, "claude", "not_working"),
        (SessionIntelligence.AGENT_WORKING, "gemini", "unsupported_runtime"),
        (SessionIntelligence.AGENT_WORKING, "shell", "unsupported_runtime"),
    ],
)
async def test_refusals_queue_nothing(config, db, working, intel, runtime, reason):
    working[0].return_value = _profile(intel, runtime)
    report = await steer_agent("ike", "x", config, db=db, sender="leo")
    assert report.outcome == "refused" and report.reason == reason
    assert await db.deliveries.query(session_name="ike") == []
    assert await db.queue.sessions_with_pending() == []
    assert not (config.state_dir / "context").exists()
    if reason in ("not_working", "unsupported_runtime"):
        assert "send an ordinary message" in report.evidence[-1]


def _hook(hook, config, payload: dict) -> None:
    with (
        patch.object(hook.sys, "stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout", new_callable=io.StringIO),
    ):
        assert hook.main(["--state-dir", str(config.state_dir), "--agent", "ike"]) == 0


@pytest.mark.parametrize(
    ("hook", "event"),
    [(claude_hook, "Stop"), (codex_hook, "Stop"), (codex_hook, "Interrupt")],
)
@pytest.mark.parametrize("next_task", [False, True])
async def test_a_turn_that_ends_while_the_offer_is_written_never_reaches_the_next_task(
    config, db, working, monkeypatch, hook, event, next_task
):
    """The turn's end retires offers before this one exists; the next task must not take it."""
    monkeypatch.delenv("BACKBONE_STATE_DIR", raising=False)
    monkeypatch.setenv("BACKBONE_LAUNCH_ID", "L1")
    prompt = {"hook_event_name": "UserPromptSubmit", "session_id": "s"}
    _hook(hook, config, {**prompt, "prompt": "task one"})

    def offer_as_the_turn_ends(*args):
        _hook(hook, config, {"hook_event_name": event, "session_id": "s"})
        if next_task:
            _hook(hook, config, {**prompt, "prompt": "task two"})
        return offer_steer(*args)

    with patch(f"{_STEER}.offer_steer", side_effect=offer_as_the_turn_ends):
        report = await steer_agent("ike", "x", config, db=db, sender="peer")
    assert report.outcome == "refused" and report.reason == "not_working"
    assert take_context(config.state_dir, "ike", launch_id="L1") == []
    rows = await db.deliveries.query(session_name="ike", kind="steer")
    assert [r["outcome"] for r in rows] == ["not_taken"]


async def test_an_old_idle_record_does_not_withdraw_a_steer_the_terminal_shows_working(
    config, db, working
):
    """A hook file left from earlier says nothing about the turn the terminal shows."""
    config.state_dir.mkdir(parents=True, exist_ok=True)
    (config.state_dir / "ike.json").write_text(
        json.dumps({"state": "idle", "ts": time.time() - 3600}), encoding="utf-8"
    )
    report = await steer_agent("ike", "x", config, db=db, sender="peer")
    assert report.outcome == "offered"
    assert len(take_context(config.state_dir, "ike", launch_id="L1")) == 1


async def test_a_session_without_a_launch_id_is_refused(config, db, working):
    working[1].return_value = None
    report = await steer_agent("ike", "x", config, db=db, sender="leo")
    assert report.outcome == "refused" and report.reason == "no_launch_id"


async def test_an_unwritable_offer_fails_the_row(config, db, working):
    with patch(f"{_STEER}.offer_steer", side_effect=OSError("disk")):
        report = await steer_agent("ike", "x", config, db=db, sender="leo")
    assert report.outcome == "failed" and report.reason == "offer_not_written"
    rows = await db.deliveries.query(session_name="ike", kind="steer")
    assert [r["outcome"] for r in rows] == ["failed"]


class TestSettle:
    async def test_taken_offers_are_handed_off_and_cleared(self, config, db, working):
        report = await steer_agent("ike", "x", config, db=db, sender="leo")
        assert await settle_steers(config, db) == {}  # still offered, fresh
        take_context(config.state_dir, "ike", launch_id="L1")
        assert await settle_steers(config, db) == {"handed_off": 1}
        rows = await db.deliveries.query(session_name="ike", kind="steer")
        assert rows[0]["id"] == report.delivery_id and rows[0]["outcome"] == "handed_off"
        assert steer_offers(config.state_dir) == []
        assert await settle_steers(config, db) == {}

    async def test_an_offer_nobody_took_expires_as_not_taken(self, config, db, working):
        report = await steer_agent("ike", "x", config, db=db, sender="leo")
        path = config.state_dir / "context" / "ike" / "L1" / f"{steer_key(report.delivery_id)}.md"
        old = time.time() - 1000
        os.utime(path, (old, old))
        assert await settle_steers(config, db) == {"not_taken": 1}
        rows = await db.deliveries.query(session_name="ike", kind="steer")
        assert rows[0]["outcome"] == "not_taken"
        assert not path.exists()
        assert take_context(config.state_dir, "ike", launch_id="L1") == []

    async def test_an_expired_offer_the_hook_takes_meanwhile_is_handed_off(
        self, config, db, working
    ):
        report = await steer_agent("ike", "x", config, db=db, sender="leo")
        stale = [("ike", "L1", report.delivery_id, "offered", 1000.0)]
        take_context(config.state_dir, "ike", launch_id="L1")  # after the listing
        with patch(f"{_STEER}.steer_offers", return_value=stale):
            assert await settle_steers(config, db) == {}
        assert await settle_steers(config, db) == {"handed_off": 1}

    async def test_an_offer_missed_by_the_turn_is_not_taken(self, config, db, working):
        await steer_agent("ike", "x", config, db=db, sender="leo")
        retire_steers(config.state_dir, "ike", launch_id="L1")
        assert await settle_steers(config, db) == {"not_taken": 1}
        rows = await db.deliveries.query(session_name="ike", kind="steer")
        assert rows[0]["outcome"] == "not_taken"
        assert steer_offers(config.state_dir) == []

    async def test_an_offer_removed_by_a_restart_is_cancelled_after_a_grace(
        self, config, db, working
    ):
        report = await steer_agent("ike", "x", config, db=db, sender="leo")
        (
            config.state_dir / "context" / "ike" / "L1" / f"{steer_key(report.delivery_id)}.md"
        ).unlink()
        assert await settle_steers(config, db) == {}  # a brand-new row is not judged yet
        async with db.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE deliveries SET created_at = '2020-01-01T00:00:00.000000Z'"
                    " WHERE id = :id"
                ),
                {"id": report.delivery_id},
            )
        assert await settle_steers(config, db) == {"cancelled": 1}
        rows = await db.deliveries.query(session_name="ike", kind="steer")
        assert rows[0]["outcome"] == "cancelled"

    async def test_batch_offers_are_untouched_by_steers(self, config, db, working):
        from agent_backbone.hooks.backbone_state import offer_context

        offer_context(config.state_dir, "ike", "42", "[via:gmail] batch")
        report = await steer_agent("ike", "x", config, db=db, sender="leo")
        assert await settle_steers(config, db) == {}
        assert take_context(config.state_dir, "ike", launch_id="L1") == [
            "[via:gmail] batch",
            "[via:backbone from:leo] (steer for your current task) x",
        ]
        assert await settle_steers(config, db) == {"handed_off": 1}
        assert (config.state_dir / "context" / "ike" / "42.taken").exists()  # the drain's business
        assert report.delivery_id
