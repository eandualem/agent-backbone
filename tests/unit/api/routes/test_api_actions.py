"""Tests for api/routes/actions.py — agent action log endpoint."""

from __future__ import annotations

import json
from dataclasses import replace

from agent_backbone.config import AgentStateConfig


def _set_state_dir(api_app, path: str):
    """Override agent_state.state_dir in the app config."""
    config = api_app.state.config
    api_app.state.config = replace(
        config,
        agent_state=AgentStateConfig(state_dir=path),
    )


class TestListActions:
    """Tests for GET /api/actions."""

    async def test_returns_actions_sorted_descending(
        self, api_client, auth_headers, tmp_path, api_app
    ):
        """Returns action records sorted by ts descending."""
        actions_file = tmp_path / "state" / "github-actions.jsonl"
        actions_file.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps({"ts": 1000.0, "session": "ike", "action": "opened", "issue": 42}),
            json.dumps({"ts": 2000.0, "session": "feynman", "action": "closed", "issue": 43}),
            json.dumps({"ts": 1500.0, "session": "ike", "action": "commented", "issue": 42}),
        ]
        actions_file.write_text("\n".join(lines))
        _set_state_dir(api_app, str(tmp_path / "state"))

        resp = await api_client.get("/api/actions", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert data["items"][0]["ts"] == 2000.0
        assert data["items"][1]["ts"] == 1500.0
        assert data["items"][2]["ts"] == 1000.0

    async def test_filter_by_session(self, api_client, auth_headers, tmp_path, api_app):
        """Filtering by session returns only matching records."""
        actions_file = tmp_path / "state" / "github-actions.jsonl"
        actions_file.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps({"ts": 1000.0, "session": "ike", "action": "opened", "issue": 42}),
            json.dumps({"ts": 2000.0, "session": "feynman", "action": "closed", "issue": 43}),
        ]
        actions_file.write_text("\n".join(lines))
        _set_state_dir(api_app, str(tmp_path / "state"))

        resp = await api_client.get("/api/actions?session=ike", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["session"] == "ike"

    async def test_limit_parameter(self, api_client, auth_headers, tmp_path, api_app):
        """Limit parameter caps the number of returned records."""
        actions_file = tmp_path / "state" / "github-actions.jsonl"
        actions_file.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps({"ts": float(i), "session": "ike", "action": "opened", "issue": i})
            for i in range(10)
        ]
        actions_file.write_text("\n".join(lines))
        _set_state_dir(api_app, str(tmp_path / "state"))

        resp = await api_client.get("/api/actions?limit=3", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3

    async def test_empty_when_file_missing(self, api_client, auth_headers, tmp_path, api_app):
        """Returns empty list when actions file doesn't exist."""
        _set_state_dir(api_app, str(tmp_path / "nonexistent"))

        resp = await api_client.get("/api/actions", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    async def test_skips_malformed_lines(self, api_client, auth_headers, tmp_path, api_app):
        """Malformed JSON lines are silently skipped."""
        actions_file = tmp_path / "state" / "github-actions.jsonl"
        actions_file.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps({"ts": 1000.0, "session": "ike", "action": "opened", "issue": 42}),
            "not valid json{{{",
            "",
            json.dumps({"ts": 2000.0, "session": "ike", "action": "closed", "issue": 43}),
        ]
        actions_file.write_text("\n".join(lines))
        _set_state_dir(api_app, str(tmp_path / "state"))

        resp = await api_client.get("/api/actions", headers=auth_headers)

        assert resp.status_code == 200
        assert resp.json()["total"] == 2
