"""A retained notification belongs to one registration, never a reused name."""

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.config import AgentSpec
from agent_backbone.hooks.backbone_state import append_action
from agent_backbone.models import CommentData, EventType, IssueData, IssueEvent, ParsedLabels
from agent_backbone.services.agents import AgentStore, lifecycle_lock
from agent_backbone.services.routing import safe_deliver
from agent_backbone.services.routing._router import _dispatch_comment
from agent_backbone.services.routing.models import DispatchResult
from tests.support import queue_row


@pytest.mark.parametrize(
    "runtime", ["claude", "codex", "opencode", "gemini", "deepcode", "aider", "shell"]
)
async def test_delayed_receipt_cannot_acknowledge_a_replacement_registration(
    db, tmp_path, runtime, monkeypatch
):
    store = AgentStore(db, tmp_path)
    await store.start()
    spec = AgentSpec(name="worker", dir=str(tmp_path), runtime=runtime, repo="acme/app")
    await store.register(spec)
    identity = next(
        row["report_identity"] for row in await db.agents.list() if row["name"] == "worker"
    )
    monkeypatch.setenv("BACKBONE_REGISTRATION_ID", identity)
    append_action(
        store.config.state_dir,
        "worker",
        {
            "ts": time.time() - 3600,
            "action": "comment",
            "phase": "succeeded",
            "issue": 5,
            "repo": "acme/app",
            "comment_id": 123,
            "registration_id": identity,
        },
    )
    with patch("agent_backbone.services.terminal.session_exists", return_value=False):
        await store.forget("worker")
    await store.register(spec)
    assert not await db.agents.registration_matches("worker", identity)
    event = IssueEvent(
        event_type=EventType.COMMENT_CREATED,
        issue=IssueData(
            number=5, repo_full_name="acme/app", labels=ParsedLabels(targets=["worker"])
        ),
        comment=CommentData(id=123, body="Published by the previous registration"),
    )
    with patch("agent_backbone.services.routing._router._deliver", new_callable=AsyncMock) as send:
        await _dispatch_comment(event, store.config, db, DispatchResult())
    assert not await db.acks.exists(5, "worker", repo="acme/app")
    send.assert_awaited_once()


@pytest.mark.parametrize(
    "runtime", ["claude", "codex", "opencode", "gemini", "deepcode", "aider", "shell"]
)
async def test_forget_retires_pending_data_and_stale_copies_cannot_reach_reused_name(
    db, tmp_path, runtime
):
    store = AgentStore(db, tmp_path)
    await store.start()
    spec = AgentSpec(name="worker", dir=str(tmp_path), runtime=runtime)
    await store.register(spec)
    queued = await db.queue.enqueue(session_name="worker", message="old", delivery_kind="comment")
    (leased,) = await db.queue.dequeue("worker")
    identity = leased["registration_id"]
    assert identity
    await db.outbox.plan(1, [{"session_name": "worker", "target_entity": "worker"}])
    old_plan = (await db.outbox.entries(1))[0]["delivery"]
    assert old_plan["registration_id"] == identity
    with patch("agent_backbone.services.terminal.session_exists", return_value=False):
        await store.forget("worker")
    await store.register(spec)
    assert (await queue_row(db, queued.id))["status"] == "expired"
    history = await db.deliveries.query(operation_id=queued.operation_id)
    assert any(
        row["outcome"] == "expired" and row["source"] == "registration-retired" for row in history
    )
    assert (await db.outbox.entries(1))[0]["status"] == "skipped"
    assert not await db.agents.registration_matches("worker", identity)
    with patch("agent_backbone.services.routing._delivery.send_message") as send:
        result = await safe_deliver(
            "worker",
            "old",
            store.config,
            db=db,
            requeue=False,
            queue_id=queued.id,
            registration_id=identity,
        )
        assert result.outcome == "already_delivered"
        await safe_deliver(
            "worker", "old outbox", store.config, db=db, registration_id=old_plan["registration_id"]
        )
    send.assert_not_called()


async def test_delivery_waits_for_lifecycle_mutation_before_checking_registration(db, tmp_path):
    store = AgentStore(db, tmp_path)
    await store.start()
    spec = AgentSpec(name="worker", dir=str(tmp_path), runtime="codex")
    await store.register(spec)
    queued = await db.queue.enqueue(session_name="worker", message="old", delivery_kind="comment")
    identity = (await queue_row(db, queued.id))["registration_id"]
    entered = asyncio.Event()

    async def attempt():
        entered.set()
        return await safe_deliver("worker", "old", store.config, db=db, registration_id=identity)

    with patch("agent_backbone.services.routing._delivery.send_message") as send:
        async with lifecycle_lock("worker"):
            task = asyncio.create_task(attempt())
            await entered.wait()
            await asyncio.sleep(0)
            assert not task.done()
            with patch("agent_backbone.services.terminal.session_exists", return_value=False):
                await store.forget("worker")
            await store.register(spec)
        assert (await task).outcome == "already_delivered"
    send.assert_not_called()


async def test_registration_identity_survives_configuration_updates_and_rename(db, tmp_path):
    store = AgentStore(db, tmp_path)
    await store.start()
    await store.register(AgentSpec(name="worker", dir=str(tmp_path), runtime="codex"))
    identity = next(
        row["report_identity"] for row in await db.agents.list() if row["name"] == "worker"
    )
    await store.update("worker", description="new description")
    await db.agents.rename("worker", "renamed")
    assert await db.agents.registration_matches("renamed", identity)
    assert not await db.agents.registration_matches("worker", identity)


async def test_unknown_legacy_outbox_cannot_inherit_current_registration(db, tmp_path):
    store = AgentStore(db, tmp_path)
    await store.start()
    await store.register(AgentSpec(name="worker", dir=str(tmp_path), runtime="codex"))
    with patch("agent_backbone.services.routing._delivery.send_message") as send:
        assert (
            await safe_deliver("worker", "legacy", store.config, db=db, registration_id=None)
        ).outcome == "already_delivered"
    send.assert_not_called()


async def test_recorded_acknowledgment_and_late_restore_cannot_cross_registration(db, tmp_path):
    store = AgentStore(db, tmp_path)
    await store.start()
    spec = AgentSpec(name="worker", dir=str(tmp_path))
    await store.register(spec)
    old = next(row["report_identity"] for row in await db.agents.list() if row["name"] == "worker")
    assert await db.acks.record(5, "worker", repo="acme/app", registration_id=old)
    with patch("agent_backbone.services.terminal.session_exists", return_value=False):
        await store.forget("worker")
    await store.register(spec)
    current = next(
        row["report_identity"] for row in await db.agents.list() if row["name"] == "worker"
    )
    assert not await db.acks.exists(5, "worker", repo="acme/app")
    assert not await db.acks.record(5, "worker", repo="acme/app", registration_id=old)
    assert await db.acks.record(5, "worker", repo="acme/app", registration_id=current)


@pytest.mark.parametrize("bound", [False, True])
async def test_only_current_registration_receipt_acknowledges_comment(
    db, tmp_path, monkeypatch, bound
):
    store = AgentStore(db, tmp_path)
    await store.start()
    await store.register(AgentSpec(name="worker", dir=str(tmp_path), repo="acme/app"))
    identity = next(
        row["report_identity"] for row in await db.agents.list() if row["name"] == "worker"
    )
    if bound:
        monkeypatch.setenv("BACKBONE_REGISTRATION_ID", identity)
    else:
        monkeypatch.delenv("BACKBONE_REGISTRATION_ID", raising=False)
    append_action(
        store.config.state_dir,
        "worker",
        {
            "ts": time.time(),
            "action": "comment",
            "phase": "succeeded",
            "issue": 5,
            "repo": "acme/app",
            "comment_id": 123,
        },
    )
    event = IssueEvent(
        event_type=EventType.COMMENT_CREATED,
        issue=IssueData(
            number=5, repo_full_name="acme/app", labels=ParsedLabels(targets=["worker"])
        ),
        comment=CommentData(id=123, body="Published comment"),
    )
    with patch("agent_backbone.services.routing._router._deliver", new_callable=AsyncMock) as send:
        await _dispatch_comment(event, store.config, db, DispatchResult())
    assert await db.acks.exists(5, "worker", repo="acme/app") is bound
    assert send.await_count == (0 if bound else 1)


async def test_bound_receipt_follows_the_same_registration_after_rename(db, tmp_path, monkeypatch):
    store = AgentStore(db, tmp_path)
    await store.start()
    await store.register(AgentSpec(name="worker", dir=str(tmp_path), repo="acme/app"))
    identity = next(
        row["report_identity"] for row in await db.agents.list() if row["name"] == "worker"
    )
    monkeypatch.setenv("BACKBONE_REGISTRATION_ID", identity)
    append_action(
        store.config.state_dir,
        "worker",
        {
            "ts": time.time(),
            "action": "comment",
            "phase": "succeeded",
            "issue": 5,
            "repo": "acme/app",
            "comment_id": 123,
        },
    )
    with patch("agent_backbone.services.terminal.session_exists", return_value=False):
        await store.rename("worker", "renamed")
    event = IssueEvent(
        event_type=EventType.COMMENT_CREATED,
        issue=IssueData(
            number=5, repo_full_name="acme/app", labels=ParsedLabels(targets=["renamed"])
        ),
        comment=CommentData(id=123, body="Published before rename"),
    )
    with patch("agent_backbone.services.routing._router._deliver", new_callable=AsyncMock) as send:
        await _dispatch_comment(event, store.config, db, DispatchResult())
    assert await db.acks.exists(5, "renamed", repo="acme/app")
    send.assert_not_awaited()


@pytest.mark.parametrize("retired", [False, True])
async def test_unbound_or_retired_pr_receipt_cannot_acknowledge_work(
    db, tmp_path, monkeypatch, retired
):
    from agent_backbone.services.routing._router import _dispatch_pull_request

    store = AgentStore(db, tmp_path)
    await store.start()
    spec = AgentSpec(name="worker", dir=str(tmp_path), repo="acme/app")
    await store.register(spec)
    identity = next(
        row["report_identity"] for row in await db.agents.list() if row["name"] == "worker"
    )
    monkeypatch.setenv("BACKBONE_REGISTRATION_ID", identity if retired else "")
    append_action(
        store.config.state_dir,
        "worker",
        {
            "ts": time.time(),
            "action": "pull_request",
            "phase": "succeeded",
            "repo": "acme/app",
            "branch": "feat/example",
        },
    )
    if retired:
        with patch("agent_backbone.services.terminal.session_exists", return_value=False):
            await store.forget("worker")
        await store.register(spec)
    event = IssueEvent(
        event_type=EventType.PULL_REQUEST_OPENED,
        issue=IssueData(
            number=6,
            repo_full_name="acme/app",
            head_repo="acme/app",
            head_ref="feat/example",
            body="Closes #5",
        ),
    )
    with patch("agent_backbone.services.routing._router._deliver", new_callable=AsyncMock) as send:
        await _dispatch_pull_request(event, store.config, db, DispatchResult())
    assert not await db.acks.exists(5, "worker", repo="acme/app")
    send.assert_awaited_once()


@pytest.mark.parametrize("editor_bound", [False, True])
async def test_latest_comment_edit_cannot_fall_back_to_older_author(
    db, tmp_path, monkeypatch, editor_bound
):
    from agent_backbone.services.agents import has_commented_on_issue

    store = AgentStore(db, tmp_path)
    author = await store.register(AgentSpec(name="author", dir=str(tmp_path), repo="acme/app"))
    editor = await store.register(AgentSpec(name="editor", dir=str(tmp_path), repo="acme/app"))
    for spec, timestamp, identity in (
        (author, 100, author.registration_id),
        (editor, 200, editor.registration_id if editor_bound else ""),
    ):
        monkeypatch.setenv("BACKBONE_REGISTRATION_ID", identity)
        append_action(
            store.config.state_dir,
            spec.name,
            {
                "ts": timestamp,
                "action": "comment",
                "phase": "succeeded",
                "issue": 5,
                "repo": "acme/app",
                "comment_id": 123,
            },
        )
    with patch("agent_backbone.services.terminal.session_exists", return_value=False):
        await store.forget(editor.name)
    event = IssueEvent(
        event_type=EventType.COMMENT_CREATED,
        issue=IssueData(
            number=5, repo_full_name="acme/app", labels=ParsedLabels(targets=[author.name])
        ),
        comment=CommentData(id=123, body="Later edit"),
    )
    with patch("agent_backbone.services.routing._router._deliver", new_callable=AsyncMock) as send:
        await _dispatch_comment(event, store.config, db, DispatchResult())
    assert not await db.acks.exists(5, author.name, repo="acme/app")
    send.assert_awaited_once()
    _cleared, comment_id = await db.acks.reset_boundary(5, author.name, repo="acme/app")
    assert not has_commented_on_issue(
        5,
        author.name,
        store.config.action_log_path,
        repo="acme/app",
        registration_id=author.registration_id,
        since=200,
        reset_comment_id=comment_id,
    )


async def test_delayed_own_comment_keeps_attribution_without_restoring_reset_ack(
    db, tmp_path, monkeypatch
):
    store = AgentStore(db, tmp_path)
    spec = await store.register(AgentSpec(name="worker", dir=str(tmp_path), repo="acme/app"))
    monkeypatch.setenv("BACKBONE_REGISTRATION_ID", spec.registration_id)
    append_action(
        store.config.state_dir,
        spec.name,
        {
            "ts": time.time(),
            "action": "comment",
            "phase": "succeeded",
            "issue": 5,
            "repo": "acme/app",
            "comment_id": 123,
        },
    )
    await db.acks.clear(5, spec.name, repo="acme/app", comment_id=124)
    event = IssueEvent(
        event_type=EventType.COMMENT_CREATED,
        issue=IssueData(
            number=5, repo_full_name="acme/app", labels=ParsedLabels(targets=[spec.name])
        ),
        comment=CommentData(id=123, body="Older own comment"),
    )
    with patch("agent_backbone.services.routing._router._deliver", new_callable=AsyncMock) as send:
        await _dispatch_comment(event, store.config, db, DispatchResult())
    assert not await db.acks.exists(5, spec.name, repo="acme/app")
    send.assert_not_awaited()
