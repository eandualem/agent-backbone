"""Scenario-based delivery tests covering end-to-end routing contracts."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from agent_backbone.config import BackboneConfig
from agent_backbone.models import EventType, IssueData, IssueEvent, ParsedLabels
from agent_backbone.services.agents.models import AgentState
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.registry import EntityEntry, EntityRegistry, RepoInfo
from agent_backbone.services.routing import clear as clear_notification_dedup
from agent_backbone.services.routing import issue_dispatcher, on_issue_closed, retry_delivery
from agent_backbone.services.routing._flows import drain_message_queue
from agent_backbone.services.routing.models import SessionIntelligence, SessionProfile


def _issue(
    number: int,
    title: str,
    *,
    sender: str = "unknown",
    targets: list[str] | None = None,
    issue_type: str = "task",
    priority: str = "",
    repo_full_name: str = "eandualem/orchestration",
    state: str = "open",
) -> IssueData:
    return IssueData(
        number=number,
        title=title,
        state=state,
        labels=ParsedLabels(
            sender=sender,
            targets=list(targets or []),
            issue_type=issue_type,
            priority=priority,
        ),
        html_url=f"https://github.com/{repo_full_name}/issues/{number}",
        repo_full_name=repo_full_name,
    )


def _event(event_type: EventType, issue: IssueData) -> IssueEvent:
    return IssueEvent(event_type=event_type, issue=issue)


def _profile(
    session_name: str,
    intelligence: SessionIntelligence,
    *,
    current_issue: int | None = None,
) -> SessionProfile:
    agent_state = {
        SessionIntelligence.IDLE_READY: AgentState.IDLE,
        SessionIntelligence.IDLE_GRACE: AgentState.IDLE,
        SessionIntelligence.COPY_MODE: AgentState.IDLE,
        SessionIntelligence.USER_INTERACTING: AgentState.IDLE,
        SessionIntelligence.AGENT_WORKING: AgentState.PROCESSING_ISSUE,
        SessionIntelligence.PLAN_WAITING: AgentState.PLAN_WAITING,
        SessionIntelligence.PERMISSION_WAITING: AgentState.PERMISSION_WAITING,
        SessionIntelligence.OFFLINE: AgentState.UNKNOWN,
        SessionIntelligence.UNKNOWN: AgentState.UNKNOWN,
    }[intelligence]
    return SessionProfile(
        session_name=session_name,
        intelligence=intelligence,
        runtime="codex",
        agent_state=agent_state,
        current_issue=current_issue,
    )


@contextmanager
def _patch_delivery_runtime(
    profiles: dict[str, SessionIntelligence | tuple[SessionIntelligence, int | None]],
    sent_messages: list[tuple[str, str, str]],
):
    async def _get_session_intelligence(session_name: str, *_args: Any, **_kwargs: Any):
        value = profiles.get(session_name)
        if value is None:
            raise AssertionError(f"Unexpected session lookup: {session_name}")
        if isinstance(value, tuple):
            intelligence, current_issue = value
        else:
            intelligence, current_issue = value, None
        return _profile(session_name, intelligence, current_issue=current_issue)

    async def _send_message(session_name: str, message: str, runtime_hint: str = "unknown") -> bool:
        sent_messages.append((session_name, message, runtime_hint))
        return True

    with (
        patch(
            "agent_backbone.services.routing._delivery.get_session_intelligence",
            new_callable=AsyncMock,
            side_effect=_get_session_intelligence,
        ),
        patch(
            "agent_backbone.services.routing._delivery.send_message",
            new_callable=AsyncMock,
            side_effect=_send_message,
        ),
    ):
        yield


@contextmanager
def _patch_session_exists(active_sessions: set[str]):
    async def _exists(session_name: str) -> bool:
        return session_name in active_sessions

    with (
        patch(
            "agent_backbone.services.routing._resolution.session_exists",
            new_callable=AsyncMock,
            side_effect=_exists,
        ),
        patch(
            "agent_backbone.services.routing._lifecycle.session_exists",
            new_callable=AsyncMock,
            side_effect=_exists,
        ),
    ):
        yield


@pytest.fixture(autouse=True)
def _clear_recent_notification_dedup():
    clear_notification_dedup()
    yield
    clear_notification_dedup()


@pytest.fixture
def delivery_config() -> BackboneConfig:
    return BackboneConfig(
        webhook_secret="test-secret",
        registry=EntityRegistry(
            entities={
                "leo": EntityEntry(
                    session="leo",
                    home="~/ws/leo",
                    groups=["orchestrators"],
                    figure="Leonardo da Vinci",
                    role="Strategy Co-Architect",
                ),
                "ike": EntityEntry(
                    session="ike",
                    home="~/ws/core/ike",
                    groups=["orchestrators"],
                    figure="Dwight Eisenhower",
                    role="Core Orchestrator",
                ),
                "bell-wf": EntityEntry(
                    session="bell-wf",
                    home="~/ws/core/code/WF/bell",
                    groups=["orchestrators"],
                    figure="Alexander Graham Bell",
                    role="Org Orchestrator",
                    organization="WF",
                    entity_type="role-instance",
                ),
            },
            repos=[
                RepoInfo(
                    org="WF",
                    name="agent-orchestration-dashboard",
                    path="/tmp/agent-orchestration-dashboard",
                ),
                RepoInfo(
                    org="WF",
                    name="agent-backbone",
                    path="/tmp/agent-backbone",
                ),
            ],
        ),
    )


@pytest.fixture
async def scenario_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    db = BackboneDB(engine)
    await db.start()
    try:
        yield db
    finally:
        db._engine = None
        await engine.dispose()


@pytest.fixture
async def delivery_api_client(api_app, delivery_config):
    api_app.state.config = delivery_config
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, api_app.state.db


class TestDeliveryScenarios:
    async def test_issue_dispatcher_routes_cross_repo_targets_end_to_end(
        self,
        delivery_config,
        scenario_db,
    ):
        bell_issue = _issue(
            41,
            "[task] Verify delivery status",
            sender="leo",
            targets=["bell-wf"],
        )
        repo_issue = _issue(
            20,
            "Dashboard regression in issue feed",
            issue_type="",
            repo_full_name="eandualem/agent-orchestration-dashboard",
        )
        coding_issue = _issue(
            42,
            "[task] agent-orchestration-dashboard: investigate queue replay",
            sender="bell-wf",
            targets=["coding-agent"],
        )
        gh = AsyncMock()

        async def _list_open_issues(label: str, repo_full_name: str | None = None):
            if (label, repo_full_name) == ("for:bell-wf", "eandualem/orchestration"):
                return [bell_issue]
            if (label, repo_full_name) == ("for:coding-agent", "eandualem/orchestration"):
                return [coding_issue]
            raise AssertionError(f"Unexpected list_open_issues call: {label} {repo_full_name}")

        async def _list_issues(*, state: str = "open", repo_full_name: str | None = None, **_):
            if (state, repo_full_name) == ("open", "eandualem/agent-orchestration-dashboard"):
                return [repo_issue]
            raise AssertionError(f"Unexpected list_issues call: {state} {repo_full_name}")

        gh.list_open_issues = AsyncMock(side_effect=_list_open_issues)
        gh.list_issues = AsyncMock(side_effect=_list_issues)

        sent_messages: list[tuple[str, str, str]] = []
        with (
            _patch_session_exists({"agent-orchestration-dashboard"}),
            _patch_delivery_runtime(
                {
                    "bell-wf": SessionIntelligence.IDLE_READY,
                    "agent-orchestration-dashboard": SessionIntelligence.IDLE_READY,
                },
                sent_messages,
            ),
        ):
            orchestration_result = await issue_dispatcher.fn(
                _event(EventType.ISSUE_OPENED, bell_issue),
                delivery_config,
                scenario_db,
                gh,
            )
            repo_result = await issue_dispatcher.fn(
                _event(EventType.ISSUE_OPENED, repo_issue),
                delivery_config,
                scenario_db,
                gh,
            )
            coding_result = await issue_dispatcher.fn(
                _event(EventType.ISSUE_OPENED, coding_issue),
                delivery_config,
                scenario_db,
                gh,
            )

        assert orchestration_result.delivered == ["bell-wf"]
        assert repo_result.delivered == ["agent-orchestration-dashboard"]
        assert coding_result.delivered == ["agent-orchestration-dashboard"]

        assert sent_messages[0][0] == "bell-wf"
        assert "gh issue view 41 --repo eandualem/orchestration" in sent_messages[0][1]
        assert sent_messages[1][0] == "agent-orchestration-dashboard"
        assert "gh issue view 20 --repo eandualem/agent-orchestration-dashboard" in sent_messages[1][1]
        assert sent_messages[2][0] == "agent-orchestration-dashboard"
        assert "gh issue view 42 --repo eandualem/orchestration" in sent_messages[2][1]
        assert all("mcp__github__" not in message for _, message, _ in sent_messages)

        deliveries = await scenario_db.query_deliveries(limit=10)
        recorded = {
            (row["repo_full_name"], row["issue_number"], row["session_name"], row["outcome"])
            for row in deliveries
        }
        assert (
            "eandualem/orchestration",
            41,
            "bell-wf",
            "delivered",
        ) in recorded
        assert (
            "eandualem/agent-orchestration-dashboard",
            20,
            "agent-orchestration-dashboard",
            "delivered",
        ) in recorded
        assert (
            "eandualem/orchestration",
            42,
            "agent-orchestration-dashboard",
            "delivered",
        ) in recorded

    @pytest.mark.parametrize(
        ("initial_intelligence", "expected_outcome"),
        [
            (SessionIntelligence.AGENT_WORKING, "agent_working"),
            (SessionIntelligence.PLAN_WAITING, "plan_waiting"),
        ],
    )
    async def test_issue_delivery_gating_retries_once_idle(
        self,
        delivery_config,
        scenario_db,
        initial_intelligence,
        expected_outcome,
    ):
        issue = _issue(
            31,
            "Queue health check",
            issue_type="",
            repo_full_name="eandualem/agent-orchestration-dashboard",
        )
        gh = AsyncMock()
        gh.list_issues = AsyncMock(return_value=[issue])

        sent_messages: list[tuple[str, str, str]] = []
        with (
            _patch_session_exists({"agent-orchestration-dashboard"}),
            _patch_delivery_runtime(
                {"agent-orchestration-dashboard": initial_intelligence},
                sent_messages,
            ),
        ):
            result = await issue_dispatcher.fn(
                _event(EventType.ISSUE_OPENED, issue),
                delivery_config,
                scenario_db,
                gh,
            )

        assert result.deferred == ["agent-orchestration-dashboard"]
        assert sent_messages == []

        failed = await scenario_db.get_failed_deliveries(limit=10)
        assert len(failed) == 1
        failed_row = failed[0]
        assert failed_row["repo_full_name"] == "eandualem/agent-orchestration-dashboard"
        assert failed_row["issue_number"] == 31
        assert failed_row["outcome"] == expected_outcome

        gh.get_issue = AsyncMock(return_value=issue)
        gh.list_issues = AsyncMock(return_value=[issue])

        with _patch_delivery_runtime(
            {"agent-orchestration-dashboard": SessionIntelligence.IDLE_READY},
            sent_messages,
        ):
            retry_outcome = await retry_delivery.fn(
                delivery_config,
                failed_row,
                scenario_db,
                gh,
                active_sessions={"agent-orchestration-dashboard"},
            )

        assert retry_outcome == "retried"
        assert sent_messages[-1][0] == "agent-orchestration-dashboard"
        assert "gh issue view 31 --repo eandualem/agent-orchestration-dashboard" in sent_messages[-1][1]

        remaining_failed = await scenario_db.get_failed_deliveries(limit=10)
        assert remaining_failed == []

    async def test_direct_messages_honor_priority_queue_fifo_and_from_entity(
        self,
        delivery_api_client,
        delivery_config,
        auth_headers,
    ):
        client, db = delivery_api_client
        sent_messages: list[tuple[str, str, str]] = []

        with _patch_delivery_runtime({"ike": SessionIntelligence.IDLE_READY}, sent_messages):
            resp = await client.post(
                "/api/messages",
                headers=auth_headers,
                json={
                    "target_session": "ike",
                    "from_entity": "bell-wf",
                    "message": "priority while idle",
                    "priority": True,
                },
            )

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["outcome"] == "delivered"
        assert sent_messages == [
            ("ike", "[via:backbone from:bell-wf] priority while idle", "codex")
        ]

        sent_messages.clear()
        queued_payloads = [
            ("urgent but busy", True, "agent_working"),
            ("fifo-1", False, "agent_working"),
            ("fifo-2", False, "agent_working"),
        ]
        with _patch_delivery_runtime({"ike": SessionIntelligence.AGENT_WORKING}, sent_messages):
            for message, priority, expected_outcome in queued_payloads:
                resp = await client.post(
                    "/api/messages",
                    headers=auth_headers,
                    json={
                        "target_session": "ike",
                        "from_entity": "bell-wf",
                        "message": message,
                        "priority": priority,
                    },
                )
                assert resp.status_code == 200
                assert resp.json()["ok"] is False
                assert resp.json()["outcome"] == expected_outcome

        with _patch_delivery_runtime({"ike": SessionIntelligence.OFFLINE}, sent_messages):
            offline_resp = await client.post(
                "/api/messages",
                headers=auth_headers,
                json={
                    "target_session": "ike",
                    "from_entity": "bell-wf",
                    "message": "offline for later",
                },
            )

        assert offline_resp.status_code == 200
        assert offline_resp.json()["ok"] is False
        assert offline_resp.json()["outcome"] == "offline"
        assert sent_messages == []
        assert await db.get_sessions_with_pending() == ["ike"]

        with _patch_delivery_runtime({"ike": SessionIntelligence.IDLE_READY}, sent_messages):
            summary = await drain_message_queue(
                delivery_config,
                db,
                AsyncMock(),
                active_sessions={"ike"},
            )

        assert summary["queue_delivered"] == 4
        assert [message for _, message, _ in sent_messages] == [
            "[via:backbone from:bell-wf] urgent but busy",
            "[via:backbone from:bell-wf] fifo-1",
            "[via:backbone from:bell-wf] fifo-2",
            "[via:backbone from:bell-wf] offline for later",
        ]

    async def test_acknowledgment_persists_without_cross_repo_collisions(
        self,
        delivery_config,
        scenario_db,
    ):
        orchestration_issue = _issue(
            42,
            "[task] agent-orchestration-dashboard: revisit ack persistence",
            sender="leo",
            targets=["coding-agent"],
        )
        repo_issue = _issue(
            42,
            "Repo-local ack persistence follow-up",
            issue_type="",
            repo_full_name="eandualem/agent-orchestration-dashboard",
        )
        dispatch_gh = AsyncMock()
        dispatch_gh.list_open_issues = AsyncMock(return_value=[orchestration_issue])
        dispatch_gh.list_issues = AsyncMock(return_value=[repo_issue])

        sent_messages: list[tuple[str, str, str]] = []
        with (
            _patch_session_exists({"agent-orchestration-dashboard"}),
            _patch_delivery_runtime(
                {"agent-orchestration-dashboard": SessionIntelligence.OFFLINE},
                sent_messages,
            ),
        ):
            await issue_dispatcher.fn(
                _event(EventType.ISSUE_OPENED, orchestration_issue),
                delivery_config,
                scenario_db,
                dispatch_gh,
            )
            await issue_dispatcher.fn(
                _event(EventType.ISSUE_OPENED, repo_issue),
                delivery_config,
                scenario_db,
                dispatch_gh,
            )

        await scenario_db.record_acknowledgment("eandualem/orchestration", 42, "coding-agent")

        failed_rows = {
            (row["repo_full_name"], row["target_entity"]): row
            for row in await scenario_db.get_failed_deliveries(limit=10)
        }
        assert (
            "eandualem/orchestration",
            "coding-agent",
        ) in failed_rows
        assert (
            "eandualem/agent-orchestration-dashboard",
            "agent-orchestration-dashboard",
        ) in failed_rows

        retry_gh = AsyncMock()

        async def _get_issue(issue_number: int, repo_full_name: str | None = None):
            if (issue_number, repo_full_name) == (42, "eandualem/agent-orchestration-dashboard"):
                return repo_issue
            if (issue_number, repo_full_name) == (42, "eandualem/orchestration"):
                return orchestration_issue
            raise AssertionError(f"Unexpected get_issue call: {issue_number} {repo_full_name}")

        retry_gh.get_issue = AsyncMock(side_effect=_get_issue)
        retry_gh.list_open_issues = AsyncMock(return_value=[orchestration_issue])
        retry_gh.list_issues = AsyncMock(return_value=[repo_issue])

        sent_messages.clear()
        with _patch_delivery_runtime(
            {"agent-orchestration-dashboard": SessionIntelligence.IDLE_READY},
            sent_messages,
        ):
            orchestration_outcome = await retry_delivery.fn(
                delivery_config,
                failed_rows[("eandualem/orchestration", "coding-agent")],
                scenario_db,
                retry_gh,
                active_sessions={"agent-orchestration-dashboard"},
            )
            repo_outcome = await retry_delivery.fn(
                delivery_config,
                failed_rows[
                    (
                        "eandualem/agent-orchestration-dashboard",
                        "agent-orchestration-dashboard",
                    )
                ],
                scenario_db,
                retry_gh,
                active_sessions={"agent-orchestration-dashboard"},
            )

        assert orchestration_outcome == "acknowledged"
        assert repo_outcome == "retried"
        assert len(sent_messages) == 1
        assert "gh issue view 42 --repo eandualem/agent-orchestration-dashboard" in sent_messages[0][1]
        assert await scenario_db.is_acknowledged(
            "eandualem/agent-orchestration-dashboard",
            42,
            "agent-orchestration-dashboard",
        ) is False

    async def test_failed_deliveries_api_returns_repo_qualified_records(
        self,
        delivery_api_client,
        auth_headers,
    ):
        client, db = delivery_api_client

        await db.record_delivery(
            "eandualem/orchestration",
            91,
            "ike",
            "ike",
            "offline",
            "issue-dispatcher",
        )
        await db.record_delivery(
            "eandualem/agent-orchestration-dashboard",
            20,
            "agent-orchestration-dashboard",
            "agent-orchestration-dashboard",
            "delivery_failed",
            "issue-dispatcher",
        )
        await db.record_delivery(
            "eandualem/orchestration",
            92,
            "ike",
            "ike",
            "delivered",
            "issue-dispatcher",
        )

        resp = await client.get("/api/deliveries/failed", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert {
            (item["repo_full_name"], item["issue_number"], item["outcome"])
            for item in body["items"]
        } == {
            ("eandualem/orchestration", 91, "offline"),
            ("eandualem/agent-orchestration-dashboard", 20, "delivery_failed"),
        }

        alias_resp = await client.get("/api/failed-deliveries", headers=auth_headers)
        assert alias_resp.status_code == 200
        assert alias_resp.json() == body

    async def test_close_then_next_delivers_repo_local_blocking_issue_and_purges_closed_queue(
        self,
        delivery_config,
        scenario_db,
    ):
        closed_issue = _issue(
            20,
            "Finished dashboard fix",
            issue_type="",
            repo_full_name="eandualem/agent-orchestration-dashboard",
            state="closed",
        )
        blocking_issue = _issue(
            21,
            "[bug] Blocking dashboard follow-up",
            sender="leo",
            targets=["agent-orchestration-dashboard"],
            issue_type="bug",
            priority="blocking",
            repo_full_name="eandualem/agent-orchestration-dashboard",
        )
        normal_issue = _issue(
            22,
            "[task] Later dashboard cleanup",
            sender="leo",
            targets=["agent-orchestration-dashboard"],
            repo_full_name="eandualem/agent-orchestration-dashboard",
        )
        stale_message_id = await scenario_db.enqueue_message(
            session_name="agent-orchestration-dashboard",
            message="stale queue entry",
            repo_full_name="eandualem/agent-orchestration-dashboard",
            issue_number=20,
            target_entity="agent-orchestration-dashboard",
            flow_name="issue-dispatcher",
        )

        gh = AsyncMock()
        gh.list_issues = AsyncMock(return_value=[blocking_issue, normal_issue])

        sent_messages: list[tuple[str, str, str]] = []
        with (
            _patch_session_exists({"agent-orchestration-dashboard"}),
            _patch_delivery_runtime(
                {"agent-orchestration-dashboard": SessionIntelligence.IDLE_READY},
                sent_messages,
            ),
        ):
            result = await on_issue_closed.fn(
                _event(EventType.ISSUE_CLOSED, closed_issue),
                delivery_config,
                gh,
                scenario_db,
            )

        assert result["agent-orchestration-dashboard"] == "delivered_#21"
        assert len(sent_messages) == 1
        assert sent_messages[0][0] == "agent-orchestration-dashboard"
        assert "[blocking]" in sent_messages[0][1]
        assert "gh issue view 21 --repo eandualem/agent-orchestration-dashboard" in sent_messages[0][1]
        assert "mcp__github__" not in sent_messages[0][1]

        purged_row = await scenario_db.get_message_by_id(stale_message_id)
        assert purged_row is not None
        assert purged_row["status"] == "delivered"
