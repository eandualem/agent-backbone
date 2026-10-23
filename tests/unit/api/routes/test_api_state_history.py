"""Observed state history uses the reconciled feed and a bounded, read-only API."""

import time
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.api.session_updates import SessionFeed
from agent_backbone.config import AgentsConfig, AgentSpec
from agent_backbone.services.agents import write_state_file

_PATH = "/api/agents/states"
_SINCE = "2000-01-01T00:00:00Z"
_CLOCK = "agent_backbone.services.database._state_history_repo.now_iso"


@pytest.mark.parametrize(
    "runtime", ["claude", "codex", "opencode", "gemini", "deepcode", "aider", "shell"]
)
async def test_history_across_runtimes(api_app, api_client, auth_headers, runtime):
    config = replace(
        api_app.state.config,
        agents=AgentsConfig(
            specs={"worker": AgentSpec(name="worker", dir="/project", runtime=runtime)}
        ),
    )
    api_app.state.config = config
    # No broadcaster or connected client: monitor refreshes still record history.
    feed = SessionFeed(lambda: config, db=api_app.state.db)
    api_app.state.feed = feed
    with (
        patch(
            "agent_backbone.services.agents.queries.list_sessions_rich",
            AsyncMock(return_value=[{"name": "worker"}, {"name": "unregistered"}]),
        ) as sessions,
        patch(
            "agent_backbone.services.agents.queries.query_environment_var",
            AsyncMock(return_value=None),
        ),
        patch("agent_backbone.services.agents._inference.capture_pane", AsyncMock(return_value="")),
        patch(_CLOCK, return_value="2026-10-23T14:00:00.000000Z"),
    ):
        for state, reason in [
            ("busy", None),
            ("busy", None),
            ("waiting_for_human", "permission"),
            ("waiting_for_human", "question"),
            ("idle", None),
        ]:
            write_state_file(
                config.state_dir, "worker", {"state": state, "reason": reason, "ts": time.time()}
            )
            assert await feed.emit() is False
        sessions.return_value = []
        await feed.emit()
        # A replacement feed must use durable dedup, not its own empty cache.
        await SessionFeed(lambda: config, db=api_app.state.db).emit()
        calls = sessions.await_count
        response = await api_client.get(_PATH, params={"since": _SINCE}, headers=auth_headers)
        assert sessions.await_count == calls  # History GET does not observe or refresh.
    assert response.status_code == 200
    body = response.json()
    assert [(r["state"], r["reason"]) for r in body["items"]] == [
        ("offline", None),
        ("idle", None),
        ("waiting_for_human", "question"),
        ("waiting_for_human", "permission"),
        ("busy", None),
    ]
    assert body["has_more"] is False and body["next_before_id"] is None
    for row in body["items"]:
        assert set(row) == {"id", "agent", "state", "reason", "at"}
        assert row["agent"] == "worker"
        assert row["at"] == "2026-10-23T14:00:00.000000Z"


async def test_filtered_stable_pages(api_app, api_client, auth_headers):
    history = api_app.state.db.state_history
    with patch(_CLOCK, return_value="2026-10-23T13:59:59.000000Z"):
        await history.observe([("worker", "starting", None)])
    with patch(_CLOCK, return_value="2026-10-23T14:00:00.000000Z"):
        await history.observe([("worker", "busy", None), ("other", "idle", None)])
        await history.observe([("worker", "blocked", "provider")])
    params = {"since": "2026-10-23T17:00:00+03:00", "agent": "worker", "limit": 1}
    first = (await api_client.get(_PATH, params=params, headers=auth_headers)).json()
    assert first["has_more"] and first["items"][0]["state"] == "blocked"
    assert first["next_before_id"] == first["items"][0]["id"]
    await history.observe([("worker", "unknown", None)])
    params["before_id"] = first["next_before_id"]
    second = (await api_client.get(_PATH, params=params, headers=auth_headers)).json()
    assert [r["state"] for r in second["items"]] == ["busy"]
    assert second["has_more"] is False and second["next_before_id"] is None
    empty = await api_client.get(
        _PATH, params={"since": _SINCE, "agent": "absent"}, headers=auth_headers
    )
    assert empty.json() == {"items": [], "has_more": False, "next_before_id": None}
    assert (await api_client.get(_PATH, params={"since": _SINCE})).status_code == 401


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"since": "yesterday"},
        {"since": "2026-10-23T00:00:00"},
        {"since": _SINCE, "limit": 0},
        {"since": _SINCE, "limit": 501},
        {"since": _SINCE, "before_id": 0},
        {"since": _SINCE, "agent": ""},
    ],
)
async def test_invalid_history_filters(api_client, auth_headers, params):
    assert (await api_client.get(_PATH, params=params, headers=auth_headers)).status_code == 422
