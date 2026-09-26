"""Inbox-only agents (#309): registered, never launched or typed into, their
direct messages held for ``backbone inbox``."""

from __future__ import annotations

import time
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from agent_backbone.config import AgentsConfig, AgentSpec, agents_from_rows
from agent_backbone.models import DeliveryOutcome, EventType, IssueData, ParsedLabels
from agent_backbone.services.agents import AgentState, AgentStore, agent_state, write_state_file
from agent_backbone.services.agents._validation import validate_agent_spec
from agent_backbone.services.agents.operations import (
    StartRequest,
    resolve_agent,
    start_resolved,
    stop_agent_session,
)
from agent_backbone.services.agents.queries import build_enriched_agent
from agent_backbone.services.agents.transitions import TransitionRequest, validate_transition
from agent_backbone.services.jobs import escalation as esc
from agent_backbone.services.jobs.monitor import read_states
from agent_backbone.services.jobs.retry import drain_message_queue
from agent_backbone.services.jobs.transitions import run_transitions
from agent_backbone.services.routing import route_issue, safe_deliver
from agent_backbone.services.routing._resolution import (
    is_valid_issue_target,
    resolve_entity_session,
)
from agent_backbone.services.routing._targets import issue_parties

_OPS = "agent_backbone.services.agents.operations"
_STORE = "agent_backbone.services.agents.store"
_DELIVERY = "agent_backbone.services.routing._delivery"


def _inbox_only(config, name: str = "ike"):
    agents = {spec.name: spec for spec in config.agents}
    agents[name] = replace(agents[name], inbox_only=True, repo="", watches=())
    return replace(config, agents=AgentsConfig(specs=agents))


async def _store(db, tmp_path) -> AgentStore:
    store = AgentStore(db, tmp_path)
    await store.start()
    return store


async def _register_client(store, tmp_path) -> None:
    with patch(f"{_STORE}.session_exists", AsyncMock(return_value=False)):
        await store.register(AgentSpec(name="client", dir=str(tmp_path), inbox_only=True))


async def test_the_flag_round_trips_and_defaults_off(db):
    common = dict(runtime="claude", model=None, repo="", tags=[], env={}, description="")
    await db.agents.upsert("app", dir="/a", unattended=False, **common)
    await db.agents.upsert("client", dir="/c", unattended=False, inbox_only=True, **common)
    agents = agents_from_rows(await db.agents.list())
    assert agents.get("client").inbox_only and not agents.get("app").inbox_only


async def test_start_inbox_only_registers_without_launching(db, tmp_path):
    store = await _store(db, tmp_path)
    req = StartRequest(name="client", directory=str(tmp_path), inbox_only=True)
    with (
        patch(f"{_STORE}.session_exists", AsyncMock(return_value=False)),
        patch(f"{_OPS}.launch.start_agent", AsyncMock()) as launch,
    ):
        spec = await resolve_agent(store, req)
        result = await start_resolved(store, store.config, spec, req, db=db)
    launch.assert_not_awaited()
    assert result.ok and result.ready == "inbox_only"
    assert store.agents.get("client").inbox_only


async def test_a_launch_of_an_inbox_only_agent_is_refused(db, tmp_path):
    store = await _store(db, tmp_path)
    await _register_client(store, tmp_path)
    req = StartRequest(name="client")
    with patch(f"{_OPS}.launch.start_agent", AsyncMock()) as launch:
        spec = await resolve_agent(store, req)
        with pytest.raises(ValueError, match="inbox-only"):
            await start_resolved(store, store.config, spec, req, db=db)
    launch.assert_not_awaited()


async def test_a_running_agent_is_not_made_inbox_only(db, tmp_path):
    store = await _store(db, tmp_path)
    await store.register(AgentSpec(name="app", dir=str(tmp_path), runtime="shell"))
    with patch(f"{_STORE}.session_exists", AsyncMock(return_value=True)):
        with pytest.raises(ValueError, match="stop it"):
            await store.update("app", inbox_only=True)
    assert not store.agents.get("app").inbox_only


async def test_launch_flags_do_not_apply(db, tmp_path):
    store = await _store(db, tmp_path)
    await _register_client(store, tmp_path)
    with pytest.raises(ValueError, match="always_on"):
        await store.update("client", always_on=True)


async def test_it_takes_no_subscriptions(db, tmp_path):
    store = await _store(db, tmp_path)
    await _register_client(store, tmp_path)
    with pytest.raises(ValueError, match="direct messages only"):
        await store.subscribe("client", "gmail", "from:example.com", "normal")
    await store.register(AgentSpec(name="app", dir=str(tmp_path), runtime="shell"))
    await store.subscribe("app", "gmail", "from:example.com", "normal")
    with patch(f"{_STORE}.session_exists", AsyncMock(return_value=False)):
        with pytest.raises(ValueError, match="unsubscribe"):
            await store.update("app", inbox_only=True)


async def test_a_session_with_its_name_is_not_stopped(config):
    config = _inbox_only(config)
    with patch(f"{_OPS}.launch.stop_agent", AsyncMock()) as stop:
        with pytest.raises(ValueError, match="no session to stop"):
            await stop_agent_session(config, "ike")
    stop.assert_not_awaited()


async def test_it_takes_no_part_in_github_routing(db, tmp_path):
    store = await _store(db, tmp_path)
    req = StartRequest(name="client", directory=str(tmp_path), inbox_only=True)
    with (
        patch(f"{_STORE}.detect_repo", AsyncMock(return_value="example/client")),
        patch(f"{_STORE}.session_exists", AsyncMock(return_value=False)),
    ):
        spec = await resolve_agent(store, req)
    assert spec.repo == ""
    with pytest.raises(ValueError, match="GitHub routing"):
        await store.watch("client", "example/shared")


async def test_a_pending_restart_fails_instead_of_stopping(config, db):
    config = _inbox_only(config)
    row = await db.transitions.create(agent_name="ike", delay_seconds=0)
    with patch("agent_backbone.services.jobs.transitions.launch.stop_agent", AsyncMock()) as stop:
        assert await run_transitions(lambda: config, AsyncMock(), db) == {"ike": "failed"}
    stop.assert_not_awaited()
    assert (await db.transitions.get(row["id"]))["status"] == "failed"


async def test_a_session_with_its_name_is_not_read_as_its_state(config):
    config = _inbox_only(config)
    with (
        patch("agent_backbone.services.jobs.monitor.capture_pane", AsyncMock()) as capture,
        patch("agent_backbone.services.jobs.monitor.agent_state", AsyncMock()),
    ):
        assert "ike" not in await read_states(config, {"ike", "bell"})
    assert capture.await_count == 1
    old_session = {"state": "busy", "issue": 42, "repo": "example/ike", "ts": time.time() - 60}
    write_state_file(config.state_dir, "ike", old_session)
    enriched = await build_enriched_agent("ike", config, {"ike"}, {"attached": True})
    assert not enriched.online and enriched.current_issue is None


async def test_its_state_is_never_read_from_a_session_with_its_name(
    api_app, api_client, auth_headers
):
    config = api_app.state.config = _inbox_only(api_app.state.config)
    with patch("agent_backbone.services.agents._inference.get_agent_state", AsyncMock()) as read:
        assert (await agent_state(config, "ike")).state == AgentState.UNKNOWN
    read.assert_not_awaited()
    with patch("agent_backbone.api.routes.agents.session_exists", AsyncMock(return_value=True)):
        response = await api_client.get("/api/agents/ike/inspect", headers=auth_headers)
    assert response.json()["online"] is False


def test_github_labels_do_not_route_to_it(config):
    config = _inbox_only(config)
    issue = IssueData(
        number=1,
        title="t",
        labels=ParsedLabels(sender="ike", targets=["ike"], issue_type="task"),
        repo_full_name="example/ike",
    )
    assert route_issue(issue, EventType.ISSUE_OPENED, config).queue == []
    assert issue_parties(issue, config) == []
    assert not is_valid_issue_target("ike", config)
    assert resolve_entity_session("ike", config) is None


def test_a_swarm_member_cannot_be_inbox_only(tmp_path):
    member = AgentSpec(name="m", dir=str(tmp_path), tags=("swarm:s",), inbox_only=True)
    with pytest.raises(ValueError, match="swarm member"):
        validate_agent_spec(member)


def test_a_restart_is_refused(config):
    config = _inbox_only(config)
    with pytest.raises(ValueError, match="inbox-only"):
        validate_transition(config, config.agents.get("ike"), TransitionRequest())


async def test_never_typed_into_even_when_a_session_has_its_name(config, db):
    config = _inbox_only(config)
    with (
        patch("agent_backbone.services.routing._intelligence.list_sessions", return_value={"ike"}),
        patch(f"{_DELIVERY}.send_message", AsyncMock(return_value=True)) as send,
    ):
        report = await safe_deliver(
            "ike", "[via:backbone from:tester] hi", config, db=db, delivery_kind="direct_message"
        )
    send.assert_not_awaited()
    assert report.outcome == DeliveryOutcome.OFFLINE and report.queue == "stored"
    (row,) = await db.queue.checkpoint("ike")
    assert row["message"] == "[via:backbone from:tester] hi"


async def test_its_direct_messages_wait_while_others_expire(db):
    held = await db.queue.enqueue(session_name="ike", message="to", delivery_kind="direct_message")
    await db.queue.enqueue(session_name="ike", message="issue", issue_number=7)
    await db.queue.enqueue(session_name="ike", message="batch", delivery_kind="subscription")
    await db.queue.enqueue(
        session_name="other", message="from", sender="ike", delivery_kind="direct_message"
    )
    async with db.queue._tx() as conn:
        await conn.execute(text("UPDATE message_queue SET enqueued_at = '2000-01-01T00:00:00Z'"))
    expired = await db.queue.expire_pending(inbox_sessions=("ike",))
    assert {row["message"] for row in expired} == {"issue", "batch", "from"}
    assert [row["id"] for row in await db.queue.checkpoint("ike")] == [held.id]


async def test_the_drain_leaves_its_messages_for_the_inbox(config, db):
    config = _inbox_only(config)
    await db.queue.enqueue(session_name="ike", message="hi", delivery_kind="direct_message")
    with patch("agent_backbone.services.jobs.retry.safe_deliver", AsyncMock()) as deliver:
        await drain_message_queue(config, db, None, active_sessions=set())
    deliver.assert_not_awaited()
    assert await db.queue.pending_count("ike") == 1


async def test_no_offline_queue_alert(config, db):
    config = _inbox_only(config)
    await db.queue.enqueue(session_name="ike", message="hi", delivery_kind="direct_message")
    with patch(f"{esc.__name__}.notify_humans", AsyncMock(return_value=True)) as notify:
        await esc.report_offline_queues(config, set(), db)
    notify.assert_not_awaited()


async def test_the_tell_reply_says_it_waits_in_the_inbox(api_app, api_client, auth_headers):
    api_app.state.config = _inbox_only(api_app.state.config)
    response = await api_client.post(
        "/api/messages",
        headers=auth_headers,
        json={"target_session": "ike", "from_entity": "tester", "message": "hello"},
    )
    receipt = response.json()
    assert receipt["queued"] and receipt["outcome"] == "offline"
    assert "Held for ike's inbox" in receipt["detail"]


async def test_its_prompts_are_never_answered_nor_a_terminal_read(
    api_app, api_client, auth_headers
):
    api_app.state.config = _inbox_only(api_app.state.config)
    with patch("agent_backbone.api.routes.agents.approve_agent", AsyncMock()) as approve:
        response = await api_client.post("/api/agents/ike/approve", headers=auth_headers)
    assert response.status_code == 409
    approve.assert_not_awaited()
    for path in ("terminal", "output"):
        response = await api_client.get(f"/api/sessions/ike/{path}", headers=auth_headers)
        assert response.status_code == 409


async def test_the_offline_cli_shows_no_terminal_output(config, capsys):
    import argparse

    from agent_backbone.cli import _common
    from agent_backbone.cli.agents import _agent_output

    args = argparse.Namespace(name="ike", lines=5, since=None, before=None, end=None, screen=True)
    with (
        patch.object(_common, "read_client_config", AsyncMock()),
        patch.object(_common, "api_up", AsyncMock(return_value=False)),
        patch.object(_common, "read_config", AsyncMock(return_value=_inbox_only(config))),
        patch("agent_backbone.services.agents.transcript.output_page", AsyncMock()) as read,
    ):
        assert await _agent_output(args) == 1
    read.assert_not_awaited()
    assert "inbox-only" in capsys.readouterr().out
