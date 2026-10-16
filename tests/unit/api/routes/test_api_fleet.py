"""Fleet snapshot routes: save (and stop), list, show, resume."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.services.agents import StateSnapshot
from agent_backbone.services.agents.models import AgentState

_FLEET = "agent_backbone.services.agents.fleet"


@pytest.fixture(autouse=True)
def _quiet_feed():
    with patch("agent_backbone.api.session_updates.SessionFeed.refresh_and_emit", AsyncMock()):
        yield


def _running(*names: str):
    return patch(f"{_FLEET}.session_exists", AsyncMock(side_effect=lambda n: n in names))


async def test_save_stop_list_show_and_resume(api_client, auth_headers):
    idle = AsyncMock(return_value=StateSnapshot(state=AgentState.IDLE))
    with (
        _running("ike"),
        patch(f"{_FLEET}.agent_state", idle),
        patch(f"{_FLEET}.stop_agent_session", AsyncMock(return_value=True)),
    ):
        saved = await api_client.post(
            "/api/fleet/snapshots",
            headers=auth_headers,
            json={"stop": True, "note": "night", "from_entity": "orch"},
        )
    assert saved.status_code == 201
    body = saved.json()
    assert [a["name"] for a in body["agents"]] == ["ike"]
    assert body["agents"][0]["stop"] == "stopped"
    assert body["counts"]["stopped"] == 1 and body["created_by"] == "orch"

    listed = (await api_client.get("/api/fleet/snapshots", headers=auth_headers)).json()
    assert listed[0]["id"] == body["id"] and listed[0]["last_resume"] is None

    with _running():
        resumed = await api_client.post(
            "/api/fleet/snapshots/latest/resume", headers=auth_headers, json={}
        )
    assert resumed.status_code == 200
    # No session id was reported before the save: never a guess.
    assert resumed.json()["agents"][0]["outcome"] == "not_resumed"
    assert resumed.json()["agents"][0]["reason"] == "no_saved_session"

    shown = await api_client.get(f"/api/fleet/snapshots/{body['id']}", headers=auth_headers)
    assert len(shown.json()["resumes"]) == 1


async def test_nothing_running_is_a_conflict(api_client, auth_headers):
    with _running():
        response = await api_client.post("/api/fleet/snapshots", headers=auth_headers, json={})
    assert response.status_code == 409
    missing = await api_client.get("/api/fleet/snapshots/latest", headers=auth_headers)
    assert missing.status_code == 404
    bad = await api_client.get("/api/fleet/snapshots/yesterday", headers=auth_headers)
    assert bad.status_code == 400


async def test_fleet_routes_require_authentication(api_client):
    assert (await api_client.get("/api/fleet/snapshots")).status_code == 401
