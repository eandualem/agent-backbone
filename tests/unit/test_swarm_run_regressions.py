"""Regression cases from the three-swarm run: durable checkpoints and submission races."""

import asyncio
import time
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from agent_backbone.config import AgentsConfig, AgentSpec
from agent_backbone.hooks.backbone_state import issue_from_text
from agent_backbone.services.agents import (
    AgentState,
    StateSnapshot,
    bind_task,
    get_agent_state,
    note_submission,
    write_state_file,
)
from agent_backbone.services.routing import deliver
from agent_backbone.services.routing.models import SessionIntelligence, SessionProfile
from agent_backbone.services.runtimes import RUNTIMES, SubmissionUnconfirmed
from tests.support import queue_row


async def test_uncertain_submission_is_held_and_never_retried(db, config):
    with (
        patch(
            "agent_backbone.services.routing._delivery.get_session_intelligence",
            AsyncMock(return_value=SessionProfile("ike", SessionIntelligence.READY)),
        ),
        patch(
            "agent_backbone.services.routing._delivery.send_message",
            AsyncMock(side_effect=SubmissionUnconfirmed("accepted but receipt missing")),
        ) as send,
    ):
        first = await deliver(
            "ike", "correction", config, db=db, delivery_kind="direct_message", sender="lead"
        )
        assert first.unconfirmed and first.queued
        assert (await queue_row(db, first.queue_id))["status"] == "uncertain"
        assert await db.queue.dequeue("ike") == []
        again = await deliver(
            "ike", "correction", config, db=db, delivery_kind="direct_message", sender="lead"
        )
        assert again.queue_id == first.queue_id and again.unconfirmed
        assert send.await_count == 1
    held = await db.queue.checkpoint("ike")
    assert held[0]["status"] == "uncertain"
    await db.queue.acknowledge_checkpoint("ike", [f"{first.queue_id}:{first.operation_id}"])
    assert await db.queue.checkpoint("ike") == []


async def test_checkpoint_survives_read_response_loss_and_retains_sender(db):
    receipt = await db.queue.enqueue(
        session_name="ike",
        message="[via:backbone from:lead] revise",
        delivery_kind="direct_message",
        sender="lead",
    )
    first = await db.queue.checkpoint("ike")
    assert [r["id"] for r in first] == [receipt.id]
    assert first[0]["sender"] == "lead"
    assert [r["id"] for r in await db.queue.checkpoint("ike")] == [receipt.id]
    assert await db.queue.dequeue("ike") == []
    dup = await db.queue.enqueue(
        session_name="ike",
        message=first[0]["message"],
        delivery_kind="direct_message",
        sender="lead",
    )
    assert dup.id == receipt.id and dup.status == "already_queued"
    with pytest.raises(ValueError):
        await db.queue.acknowledge_checkpoint(
            "someone-else", [f"{receipt.id}:{receipt.operation_id}"]
        )
    assert await db.queue.acknowledge_checkpoint(
        "ike", [f"{receipt.id}:{receipt.operation_id}"]
    ) == [f"{receipt.id}:{receipt.operation_id}"]
    assert await db.queue.acknowledge_checkpoint(
        "ike", [f"{receipt.id}:{receipt.operation_id}"]
    ) == [f"{receipt.id}:{receipt.operation_id}"]
    assert await db.queue.checkpoint("ike") == []
    deliveries = await db.deliveries.query(session_name="ike")
    assert len(deliveries) == 1 and deliveries[0]["source"] == "agent-checkpoint"


async def test_checkpoint_and_terminal_lease_do_not_both_claim(db):
    receipt = await db.queue.enqueue(
        session_name="ike", message="change", delivery_kind="direct_message"
    )
    terminal, inbox = await asyncio.gather(db.queue.dequeue("ike"), db.queue.checkpoint("ike"))
    assert len(terminal) + len(inbox) == 1
    assert (terminal or inbox)[0]["id"] == receipt.id


async def test_expiry_preserves_active_swarm_and_checkpoint_but_expires_other_messages(db):
    for name in ["worker", "ordinary", "reader"]:
        await db.queue.enqueue(session_name=name, message="old", delivery_kind="direct_message")
    await db.queue.enqueue(
        session_name="lead", message="handoff", delivery_kind="direct_message", sender="worker"
    )
    await db.queue.checkpoint("reader")
    async with db.engine.begin() as conn:
        await conn.execute(text("UPDATE message_queue SET enqueued_at='2000-01-01T00:00:00Z'"))
    expired = await db.queue.expire_pending(protected_sessions=("worker",))
    assert [r["session_name"] for r in expired] == ["ordinary"]
    assert len(await db.queue.dequeue("worker")) == 1
    assert len(await db.queue.dequeue("lead")) == 1
    assert len(await db.queue.checkpoint("reader")) == 1


async def test_pre_submission_idle_cannot_admit_immediate_second_send(config):
    write_state_file(config.state_dir, "ike", {"state": "idle", "ts": time.time() - 30})
    note_submission(config.state_dir, "ike")
    snap = await get_agent_state(config.state_dir, "ike", runtime_hint="claude", pane_content="❯")
    assert snap.state == AgentState.BUSY and snap.source == "delivery"
    write_state_file(config.state_dir, "ike", {"state": "busy", "ts": time.time() + 0.001})
    assert (
        await get_agent_state(config.state_dir, "ike", pane_content="❯")
    ).state == AgentState.BUSY
    write_state_file(config.state_dir, "ike", {"state": "idle", "ts": time.time() + 0.002})
    assert (
        await get_agent_state(config.state_dir, "ike", pane_content="❯")
    ).state == AgentState.IDLE


async def test_old_idle_hook_is_not_reused_after_ack_window(config):
    write_state_file(config.state_dir, "ike", {"state": "idle", "ts": time.time() - 60})
    (config.state_dir / "ike.submitted").write_text(str(time.time() - 10))
    snap = await get_agent_state(
        config.state_dir, "ike", runtime_hint="claude", pane_content="Working (esc to interrupt)\n❯"
    )
    assert snap.state == AgentState.BUSY and snap.source == "pull"


@pytest.mark.parametrize("runtime", ["claude", "codex"])
async def test_runtime_owned_queue_never_receives_another_enter_or_escape(runtime):
    rt = RUNTIMES[runtime]
    with (
        patch("agent_backbone.services.runtimes.base.paste_message", AsyncMock(return_value=True)),
        patch(
            "agent_backbone.services.runtimes.base.press_submit", AsyncMock(return_value=True)
        ) as enter,
        patch("agent_backbone.services.terminal.press_escape", AsyncMock()) as escape,
        patch.object(type(rt), "delivery_submission_state", AsyncMock(return_value="queued")),
    ):
        assert await rt.deliver_message("fixture", "one message")
        assert enter.await_count == 1
        escape.assert_not_awaited()


async def test_observed_monthly_spend_banner_blocks_fresh_idle_and_later_success_clears(config):
    banner = (
        "⎿ You've hit your monthly spend limit · raise it at "
        "claude.ai/settings/usage?from=cc_cli_limit_message · "
        "your session limit resets 1:50am (Africa/Addis_Ababa)"
    )
    write_state_file(config.state_dir, "ike", {"state": "idle", "ts": time.time()})
    snap = await get_agent_state(
        config.state_dir, "ike", runtime_hint="claude", pane_content=banner + "\n❯"
    )
    assert snap.state == AgentState.BLOCKED and "monthly spend" in snap.detail
    recovered = await get_agent_state(
        config.state_dir,
        "ike",
        runtime_hint="claude",
        pane_content=banner + "\n● Fresh successful reply\n❯",
    )
    assert recovered.state == AgentState.IDLE
    assert RUNTIMES["claude"].provider_failure("The example says " + banner) is None


def test_color_is_not_issue_and_explicit_swarm_task_wins(config):
    assert issue_from_text("Use background #444444 for the theme") == (None, None)
    assert issue_from_text("Theme issue #123") == (123, None)
    assert issue_from_text("Fix issue #444444") == (444444, None)
    assert issue_from_text("Review #123") == (123, None)
    worker = AgentSpec(name="worker", dir="/fixture", tags=("swarm:wave", "task:owner/repo#32"))
    cfg = replace(config, agents=AgentsConfig(specs={"worker": worker}))
    snap = bind_task(cfg, "worker", StateSnapshot(state=AgentState.BUSY, current_issue=8))
    assert snap.current_issue == 32 and snap.current_repo == "owner/repo"


async def test_inbox_api_claim_ack_and_unknown_agent(api_client, auth_headers, api_app):
    receipt = await api_app.state.db.queue.enqueue(
        session_name="ike",
        message="[via:backbone from:lead] correction",
        delivery_kind="direct_message",
    )
    response = await api_client.post(
        "/api/messages/inbox", headers=auth_headers, json={"session": "ike"}
    )
    assert response.status_code == 200 and response.json()["messages"][0]["id"] == receipt.id
    ack = await api_client.post(
        "/api/messages/inbox",
        headers=auth_headers,
        json={"session": "ike", "acknowledge": [f"{receipt.id}:{receipt.operation_id}"]},
    )
    assert ack.status_code == 200 and ack.json()["acknowledged"] == [
        f"{receipt.id}:{receipt.operation_id}"
    ]
    assert (
        await api_client.post(
            "/api/messages/inbox", headers=auth_headers, json={"session": "unknown"}
        )
    ).status_code == 404


async def test_different_message_waits_behind_uncertain_paste(db, config):
    held = await db.queue.enqueue(session_name="ike", message="old", delivery_kind="direct_message")
    await db.queue.hold_uncertain(held.id)
    with (
        patch(
            "agent_backbone.services.routing._delivery.get_session_intelligence",
            AsyncMock(return_value=SessionProfile("ike", SessionIntelligence.READY)),
        ),
        patch("agent_backbone.services.routing._delivery.send_message", AsyncMock()) as send,
    ):
        receipt = await deliver(
            "ike", "new correction", config, db=db, delivery_kind="direct_message"
        )
        assert receipt.outcome == "awaiting_ack" and receipt.queued
        assert receipt.queue_id != held.id
        send.assert_not_awaited()
    messages = await db.queue.checkpoint("ike")
    assert [r["message"] for r in messages] == ["old", "new correction"]


async def test_checkpoint_survives_database_restart_and_squashed_migration(tmp_path):
    from agent_backbone.services.database import BackboneDB

    url = f"sqlite+aiosqlite:///{tmp_path / 'persist.db'}"
    async with BackboneDB.connect(url) as db:
        receipt = await db.queue.enqueue(
            session_name="worker", message="durable", delivery_kind="direct_message"
        )
        await db.queue.checkpoint("worker")
    async with BackboneDB.connect(url) as db:
        (row,) = await db.queue.checkpoint("worker")
        assert row["id"] == receipt.id
        duplicate = await db.queue.enqueue(
            session_name="worker", message="durable", delivery_kind="direct_message"
        )
        assert duplicate.id == receipt.id
        await db.queue.acknowledge_checkpoint("worker", [f"{receipt.id}:{receipt.operation_id}"])


async def test_inbox_does_not_steal_issue_queue_protocol(db):
    await db.queue.enqueue(session_name="worker", message="issue", issue_number=123)
    assert await db.queue.checkpoint("worker") == []
    assert len(await db.queue.dequeue("worker")) == 1


def test_wrapped_quota_banner_and_later_output():
    rt = RUNTIMES["claude"]
    pane = (
        "  ⎿ You've hit your monthly spend limit · raise it at\n"
        "    claude.ai/settings/usage?from=cc_cli_limit_message · your session\n"
        "    limit resets 1:50am (Africa/Addis_Ababa)\n❯"
    )
    assert "monthly spend" in rt.provider_failure(pane)
    assert rt.provider_failure(pane + "\n  ● Fresh successful reply\n❯") is None


async def test_uncertain_is_visible_before_delivery_recording(db, config):
    observed = []
    record = db.deliveries.record

    async def observe(**kwargs):
        observed.extend(await db.queue.checkpoint("ike"))
        return await record(**kwargs)

    with (
        patch(
            "agent_backbone.services.routing._delivery.get_session_intelligence",
            AsyncMock(return_value=SessionProfile("ike", SessionIntelligence.READY)),
        ),
        patch(
            "agent_backbone.services.routing._delivery.send_message",
            AsyncMock(side_effect=SubmissionUnconfirmed("missing receipt")),
        ),
        patch.object(db.deliveries, "record", side_effect=observe),
    ):
        result = await deliver("ike", "fix", config, db=db, delivery_kind="direct_message")
    assert result.unconfirmed
    assert observed and all(row["status"] == "uncertain" for row in observed)
    assert await db.queue.has_uncertain("ike")


async def test_old_ack_cannot_consume_reused_sqlite_row_id(db):
    first = await db.queue.enqueue(
        session_name="worker", message="first", delivery_kind="direct_message"
    )
    (old,) = await db.queue.checkpoint("worker")
    await db.queue.acknowledge_checkpoint("worker", [old["ack_token"]])
    async with db.engine.begin() as conn:
        await conn.execute(text("UPDATE message_queue SET delivered_at='2000-01-01T00:00:00Z'"))
    assert await db.queue.prune() == 1
    second = await db.queue.enqueue(
        session_name="worker", message="second", delivery_kind="direct_message"
    )
    assert first.id == second.id  # SQLite reuses the numeric primary key.
    (new,) = await db.queue.checkpoint("worker")
    assert old["ack_token"] != new["ack_token"]
    with pytest.raises(ValueError, match="Receipts"):
        await db.queue.acknowledge_checkpoint("worker", [old["ack_token"]])
    assert (await db.queue.checkpoint("worker"))[0]["ack_token"] == new["ack_token"]


async def test_pending_duplicate_cannot_bypass_checkpoint_claim(db, config):
    receipt = await db.queue.enqueue(
        session_name="ike", message="correction", delivery_kind="direct_message"
    )
    with (
        patch("agent_backbone.services.routing._delivery.send_message", AsyncMock()) as send,
        patch(
            "agent_backbone.services.routing._delivery.get_session_intelligence",
            AsyncMock(return_value=SessionProfile("ike", SessionIntelligence.READY)),
        ),
    ):
        response, inbox = await asyncio.gather(
            deliver("ike", "correction", config, db=db, delivery_kind="direct_message"),
            db.queue.checkpoint("ike"),
        )
    assert response.queued and response.queue_id == receipt.id
    assert len(inbox) == 1
    send.assert_not_awaited()


async def test_api_checkpoint_waits_for_submission_transaction(db, config):
    from agent_backbone.services.routing import checkpoint_inbox

    pasted, finish = asyncio.Event(), asyncio.Event()

    async def uncertain(*args, **kwargs):
        pasted.set()
        await finish.wait()
        raise SubmissionUnconfirmed("missing receipt")

    with (
        patch("agent_backbone.services.routing._delivery.send_message", side_effect=uncertain),
        patch(
            "agent_backbone.services.routing._delivery.get_session_intelligence",
            AsyncMock(return_value=SessionProfile("ike", SessionIntelligence.READY)),
        ),
    ):
        delivery = asyncio.create_task(
            deliver("ike", "fix", config, db=db, delivery_kind="direct_message")
        )
        await pasted.wait()
        checkpoint = asyncio.create_task(checkpoint_inbox("ike", db=db))
        await asyncio.sleep(0)
        assert not checkpoint.done()
        finish.set()
        report, inbox = await asyncio.gather(delivery, checkpoint)
    assert report.unconfirmed and inbox["messages"][0]["status"] == "uncertain"


@pytest.mark.parametrize(
    "reply",
    [
        "● API Error: 429 means the service is rate-limiting your requests.",
        "● You have hit your usage limit in an external service.",
    ],
)
async def test_claude_explanation_is_not_provider_block(config, reply):
    write_state_file(config.state_dir, "ike", {"state": "idle", "ts": time.time()})
    state = await get_agent_state(
        config.state_dir, "ike", runtime_hint="claude", pane_content=reply + "\n❯"
    )
    assert state.state == AgentState.IDLE


@pytest.mark.parametrize(
    "message",
    [
        "The color scheme is off; please look at `#123`",
        "Use background #444444; then review #123",
        "Theme issue #123",
    ],
)
def test_color_context_does_not_hide_later_issue(message):
    assert issue_from_text(message) == (123, None)
