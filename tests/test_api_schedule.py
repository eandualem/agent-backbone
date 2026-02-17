"""Tests for api/routes/schedule.py — today's schedule endpoints."""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import patch

from src.config import AgentStateConfig, HeartbeatConfig


def _set_config_paths(api_app, schedule_path: str, state_dir: str):
    """Override heartbeat schedule path and state dir in app config."""
    config = api_app.state.config
    api_app.state.config = replace(
        config,
        heartbeat=HeartbeatConfig(schedule_file=schedule_path),
        agent_state=AgentStateConfig(state_dir=state_dir),
    )


class TestGetTodaySchedule:
    """Tests for GET /api/schedule/today."""

    async def test_returns_schedule_entries(self, api_client, auth_headers, tmp_path, api_app):
        """Returns schedule entries from heartbeat schedules with ROUTINE.md labels."""
        # Create a heartbeat schedule file
        schedule_file = tmp_path / "schedules.json"
        schedule_file.write_text(
            json.dumps(
                {
                    "leo": {
                        "cron": "0 9,17 * * *",
                        "timezone": "UTC",
                        "enabled": True,
                        "description": "Test heartbeat",
                    }
                }
            )
        )

        # Create a mock ROUTINE.md
        routine_dir = tmp_path / "routine"
        routine_dir.mkdir()
        routine_file = routine_dir / "ROUTINE.md"
        routine_content = (
            "# Leo Routine\n\n## Morning (09:00)\n- Do stuff\n\n## Evening (17:00)\n- Review\n"
        )
        routine_file.write_text(routine_content)

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        _set_config_paths(api_app, str(schedule_file), str(state_dir))

        with patch("api.routes.schedule._ROUTINE_PATHS", {"leo": str(routine_file)}):
            resp = await api_client.get("/api/schedule/today", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 2
        ids = [item["id"] for item in data["items"]]
        assert "leo-09:00" in ids
        assert "leo-17:00" in ids

        # Check labels from ROUTINE.md
        morning = next(i for i in data["items"] if i["id"] == "leo-09:00")
        assert morning["label"] == "Morning"
        assert morning["type"] == "heartbeat"
        evening = next(i for i in data["items"] if i["id"] == "leo-17:00")
        assert evening["label"] == "Evening"
        assert evening["type"] == "heartbeat"

    async def test_empty_when_no_schedules(self, api_client, auth_headers, tmp_path, api_app):
        """Returns empty list when no schedules file exists."""
        schedule_file = tmp_path / "nonexistent-schedules.json"
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        _set_config_paths(api_app, str(schedule_file), str(state_dir))

        resp = await api_client.get("/api/schedule/today", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    async def test_disabled_schedule_excluded(self, api_client, auth_headers, tmp_path, api_app):
        """Disabled schedules are not included."""
        schedule_file = tmp_path / "schedules.json"
        schedule_file.write_text(
            json.dumps(
                {
                    "ada": {
                        "cron": "0 9 * * *",
                        "timezone": "UTC",
                        "enabled": False,
                        "description": "Disabled",
                    }
                }
            )
        )
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        _set_config_paths(api_app, str(schedule_file), str(state_dir))

        resp = await api_client.get("/api/schedule/today", headers=auth_headers)

        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    async def test_done_state_applied(self, api_client, auth_headers, tmp_path, api_app):
        """Done state from file is reflected in schedule entries."""
        from datetime import datetime

        schedule_file = tmp_path / "schedules.json"
        schedule_file.write_text(
            json.dumps(
                {
                    "ike": {
                        "cron": "0 12 * * *",
                        "timezone": "UTC",
                        "enabled": True,
                        "description": "Noon check",
                    }
                }
            )
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # Write done state for today
        today = datetime.now().strftime("%Y-%m-%d")
        done_file = state_dir / f"schedule-done-{today}.json"
        done_file.write_text(json.dumps({"ike-12:00": True}))

        _set_config_paths(api_app, str(schedule_file), str(state_dir))

        with patch("api.routes.schedule._ROUTINE_PATHS", {}):
            resp = await api_client.get("/api/schedule/today", headers=auth_headers)

        assert resp.status_code == 200
        items = resp.json()["items"]
        noon = next((i for i in items if i["id"] == "ike-12:00"), None)
        assert noon is not None
        assert noon["done"] is True

    async def test_fallback_label_from_description(
        self, api_client, auth_headers, tmp_path, api_app
    ):
        """When no ROUTINE.md exists, use schedule description as label."""
        schedule_file = tmp_path / "schedules.json"
        schedule_file.write_text(
            json.dumps(
                {
                    "brunel": {
                        "cron": "0 9 * * *",
                        "timezone": "UTC",
                        "enabled": True,
                        "description": "Infra check",
                    }
                }
            )
        )
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        _set_config_paths(api_app, str(schedule_file), str(state_dir))

        with patch("api.routes.schedule._ROUTINE_PATHS", {}):
            resp = await api_client.get("/api/schedule/today", headers=auth_headers)

        assert resp.status_code == 200
        items = resp.json()["items"]
        entry = next((i for i in items if i["entity"] == "brunel"), None)
        assert entry is not None
        assert entry["label"] == "Infra check"


class TestToggleScheduleDone:
    """Tests for PATCH /api/schedule/today/{item_id}."""

    async def test_toggle_creates_done_state(self, api_client, auth_headers, tmp_path, api_app):
        """PATCH creates done state file and sets item to done."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        schedule_file = tmp_path / "schedules.json"
        schedule_file.write_text("{}")

        _set_config_paths(api_app, str(schedule_file), str(state_dir))

        resp = await api_client.patch(
            "/api/schedule/today/leo-09:00",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "leo-09:00"
        assert data["done"] is True

    async def test_toggle_off(self, api_client, auth_headers, tmp_path, api_app):
        """PATCH toggles done state off when already done."""
        from datetime import datetime

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        schedule_file = tmp_path / "schedules.json"
        schedule_file.write_text("{}")

        today = datetime.now().strftime("%Y-%m-%d")
        done_file = state_dir / f"schedule-done-{today}.json"
        done_file.write_text(json.dumps({"leo-09:00": True}))

        _set_config_paths(api_app, str(schedule_file), str(state_dir))

        resp = await api_client.patch(
            "/api/schedule/today/leo-09:00",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert resp.json()["done"] is False


class TestPersonalSchedule:
    """Tests for personal schedule item CRUD."""

    def _write_personal_schedule(self, state_dir, items):
        """Helper to write a personal-schedule.json file."""
        path = state_dir / "personal-schedule.json"
        path.write_text(json.dumps({"items": items}))

    async def test_personal_items_in_today(self, api_client, auth_headers, tmp_path, api_app):
        """GET returns personal items alongside heartbeat items."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        schedule_file = tmp_path / "schedules.json"
        schedule_file.write_text("{}")

        self._write_personal_schedule(
            state_dir,
            [
                {
                    "id": "team-standup",
                    "time": "09:30",
                    "label": "Team standup",
                    "recurring": True,
                    "done_dates": [],
                }
            ],
        )

        _set_config_paths(api_app, str(schedule_file), str(state_dir))

        with patch("api.routes.schedule._ROUTINE_PATHS", {}):
            resp = await api_client.get("/api/schedule/today", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        personal = next((i for i in data["items"] if i["id"] == "team-standup"), None)
        assert personal is not None
        assert personal["type"] == "personal"
        assert personal["entity"] == "elias"
        assert personal["time"] == "09:30"
        assert personal["label"] == "Team standup"
        assert personal["done"] is False

    async def test_recurring_day_filter(self, api_client, auth_headers, tmp_path, api_app):
        """Recurring item with days filter only shows on matching days."""
        from datetime import datetime

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        schedule_file = tmp_path / "schedules.json"
        schedule_file.write_text("{}")

        # Get today's day-of-week in Sun=0 format
        now = datetime.now()
        today_dow = (now.weekday() + 1) % 7

        # Item that does NOT match today
        other_dow = (today_dow + 1) % 7

        self._write_personal_schedule(
            state_dir,
            [
                {
                    "id": "matches-today",
                    "time": "10:00",
                    "label": "Matches today",
                    "recurring": True,
                    "days": [today_dow],
                    "done_dates": [],
                },
                {
                    "id": "wrong-day",
                    "time": "10:00",
                    "label": "Wrong day",
                    "recurring": True,
                    "days": [other_dow],
                    "done_dates": [],
                },
            ],
        )

        _set_config_paths(api_app, str(schedule_file), str(state_dir))

        with patch("api.routes.schedule._ROUTINE_PATHS", {}):
            resp = await api_client.get("/api/schedule/today", headers=auth_headers)

        assert resp.status_code == 200
        ids = [i["id"] for i in resp.json()["items"]]
        assert "matches-today" in ids
        assert "wrong-day" not in ids

    async def test_create_personal_item(self, api_client, auth_headers, tmp_path, api_app):
        """POST creates a personal schedule item."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        schedule_file = tmp_path / "schedules.json"
        schedule_file.write_text("{}")

        _set_config_paths(api_app, str(schedule_file), str(state_dir))

        resp = await api_client.post(
            "/api/schedule/personal",
            headers=auth_headers,
            json={"time": "14:00", "label": "Dentist appointment"},
        )

        assert resp.status_code == 201
        data = resp.json()
        assert data["id"] == "dentist-appointment"
        assert data["entity"] == "elias"
        assert data["type"] == "personal"
        assert data["time"] == "14:00"
        assert data["label"] == "Dentist appointment"
        assert data["done"] is False

        # Verify persisted to file
        saved = json.loads((state_dir / "personal-schedule.json").read_text())
        assert len(saved["items"]) == 1
        assert saved["items"][0]["id"] == "dentist-appointment"

    async def test_create_recurring_with_days(self, api_client, auth_headers, tmp_path, api_app):
        """POST creates a recurring item with specific days."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        schedule_file = tmp_path / "schedules.json"
        schedule_file.write_text("{}")

        _set_config_paths(api_app, str(schedule_file), str(state_dir))

        resp = await api_client.post(
            "/api/schedule/personal",
            headers=auth_headers,
            json={"time": "09:00", "label": "Gym", "recurring": True, "days": [1, 3, 5]},
        )

        assert resp.status_code == 201
        saved = json.loads((state_dir / "personal-schedule.json").read_text())
        item = saved["items"][0]
        assert item["recurring"] is True
        assert item["days"] == [1, 3, 5]

    async def test_delete_personal_item(self, api_client, auth_headers, tmp_path, api_app):
        """DELETE removes a personal schedule item."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        schedule_file = tmp_path / "schedules.json"
        schedule_file.write_text("{}")

        self._write_personal_schedule(
            state_dir,
            [
                {
                    "id": "to-delete",
                    "time": "11:00",
                    "label": "Delete me",
                    "recurring": False,
                    "done_dates": [],
                }
            ],
        )

        _set_config_paths(api_app, str(schedule_file), str(state_dir))

        resp = await api_client.delete(
            "/api/schedule/personal/to-delete",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

        # Verify removed from file
        saved = json.loads((state_dir / "personal-schedule.json").read_text())
        assert len(saved["items"]) == 0

    async def test_delete_nonexistent_returns_404(
        self, api_client, auth_headers, tmp_path, api_app
    ):
        """DELETE returns 404 for non-existent item."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        schedule_file = tmp_path / "schedules.json"
        schedule_file.write_text("{}")

        _set_config_paths(api_app, str(schedule_file), str(state_dir))

        resp = await api_client.delete(
            "/api/schedule/personal/no-such-item",
            headers=auth_headers,
        )

        assert resp.status_code == 404

    async def test_toggle_personal_done(self, api_client, auth_headers, tmp_path, api_app):
        """PATCH toggles done_dates for a personal item."""
        from datetime import datetime

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        schedule_file = tmp_path / "schedules.json"
        schedule_file.write_text("{}")

        self._write_personal_schedule(
            state_dir,
            [
                {
                    "id": "morning-run",
                    "time": "06:00",
                    "label": "Morning run",
                    "recurring": True,
                    "done_dates": [],
                }
            ],
        )

        _set_config_paths(api_app, str(schedule_file), str(state_dir))

        resp = await api_client.patch(
            "/api/schedule/today/morning-run",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert resp.json()["done"] is True

        # Verify done_dates updated in file
        today_str = datetime.now().strftime("%Y-%m-%d")
        saved = json.loads((state_dir / "personal-schedule.json").read_text())
        assert today_str in saved["items"][0]["done_dates"]

    async def test_toggle_personal_done_off(self, api_client, auth_headers, tmp_path, api_app):
        """PATCH removes today from done_dates when already done."""
        from datetime import datetime

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        schedule_file = tmp_path / "schedules.json"
        schedule_file.write_text("{}")

        today_str = datetime.now().strftime("%Y-%m-%d")
        self._write_personal_schedule(
            state_dir,
            [
                {
                    "id": "morning-run",
                    "time": "06:00",
                    "label": "Morning run",
                    "recurring": True,
                    "done_dates": [today_str],
                }
            ],
        )

        _set_config_paths(api_app, str(schedule_file), str(state_dir))

        resp = await api_client.patch(
            "/api/schedule/today/morning-run",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert resp.json()["done"] is False

        # Verify today removed from done_dates
        saved = json.loads((state_dir / "personal-schedule.json").read_text())
        assert today_str not in saved["items"][0]["done_dates"]
