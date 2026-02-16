"""Tests for flows/agent_heartbeat.py — heartbeat scheduler."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from flows.agent_heartbeat import (
    _heartbeat_lock,
    evaluate_agent_heartbeat,
    heartbeat_scheduler,
    is_due,
    load_schedules,
    save_schedules,
)
from src.agent_state import AgentState, StateSnapshot
from src.config import BackboneConfig, HeartbeatConfig

TZ = "Africa/Addis_Ababa"


# --- is_due ---


class TestIsDue:
    def test_cron_due_never_fired(self):
        """First-ever fire — no last_fired, should be due."""
        schedule = {"cron": "0 * * * *", "timezone": TZ}
        assert is_due(schedule, last_fired=None, default_tz=TZ) is True

    def test_cron_not_due_recently_fired(self):
        """Fired within current cron window — not due again."""
        schedule = {"cron": "0 * * * *", "timezone": TZ}
        tz = ZoneInfo(TZ)
        now = datetime.now(tz)
        # Set last_fired to 1 minute ago (within the current hour window)
        last_fired = (now - timedelta(minutes=1)).isoformat()
        assert is_due(schedule, last_fired=last_fired, default_tz=TZ) is False

    def test_cron_due_window_passed(self):
        """Last fired before previous fire time — due again."""
        schedule = {"cron": "0 * * * *", "timezone": TZ}
        tz = ZoneInfo(TZ)
        now = datetime.now(tz)
        # Set last_fired to 2 hours ago — at least one cron window has passed
        last_fired = (now - timedelta(hours=2)).isoformat()
        assert is_due(schedule, last_fired=last_fired, default_tz=TZ) is True

    def test_at_due(self):
        """'at' time in the past, never fired — should be due."""
        tz = ZoneInfo(TZ)
        past = (datetime.now(tz) - timedelta(hours=1)).isoformat()
        schedule = {"at": past, "timezone": TZ}
        assert is_due(schedule, last_fired=None, default_tz=TZ) is True

    def test_at_not_due_future(self):
        """'at' time in the future — not due."""
        tz = ZoneInfo(TZ)
        future = (datetime.now(tz) + timedelta(hours=1)).isoformat()
        schedule = {"at": future, "timezone": TZ}
        assert is_due(schedule, last_fired=None, default_tz=TZ) is False

    def test_at_already_fired(self):
        """'at' time in the past but already fired — not due."""
        tz = ZoneInfo(TZ)
        past = (datetime.now(tz) - timedelta(hours=1)).isoformat()
        schedule = {"at": past, "timezone": TZ}
        assert is_due(schedule, last_fired="2025-01-01T00:00:00Z", default_tz=TZ) is False

    def test_unknown_schedule_type(self):
        """No cron or at field — not due."""
        schedule = {"timezone": TZ}
        assert is_due(schedule, last_fired=None, default_tz=TZ) is False


# --- load/save schedules ---


class TestLoadSaveSchedules:
    def test_load_existing(self, tmp_path):
        path = tmp_path / "schedules.json"
        data = {"ike": {"cron": "0 8 * * *", "enabled": True}}
        path.write_text(json.dumps(data))

        result = load_schedules(path)
        assert result == data

    def test_load_missing_returns_empty(self, tmp_path):
        result = load_schedules(tmp_path / "nonexistent.json")
        assert result == {}

    def test_load_malformed_returns_empty(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not json {{{")
        assert load_schedules(path) == {}

    def test_save_roundtrip(self, tmp_path):
        path = tmp_path / "schedules.json"
        data = {"feynman": {"cron": "0 10 * * 1-5", "enabled": True}}

        save_schedules(data, path)
        loaded = load_schedules(path)
        assert loaded == data

    def test_save_atomic_creates_parent(self, tmp_path):
        path = tmp_path / "nested" / "dir" / "schedules.json"
        save_schedules({"a": 1}, path)
        assert path.exists()


# --- evaluate_agent_heartbeat ---


@pytest.fixture
def heartbeat_config(tmp_path):
    """Config with in-memory DB and tmp heartbeat schedule."""
    return BackboneConfig(
        github_token="test-token",
        webhook_secret="test-secret",
        heartbeat=HeartbeatConfig(
            schedule_file=str(tmp_path / "schedules.json"),
            default_timezone=TZ,
        ),
    )


class TestEvaluateAgentHeartbeat:
    async def test_delivers_to_idle_agent(self, heartbeat_config):
        """Happy path: due schedule, idle agent, online session."""
        schedule = {"cron": "0 * * * *", "timezone": TZ, "enabled": True}
        idle_snapshot = StateSnapshot(state=AgentState.IDLE, source="push")

        with (
            patch("flows.agent_heartbeat.get_agent_state", new_callable=AsyncMock) as mock_state,
            patch("flows.agent_heartbeat.send_message", new_callable=AsyncMock) as mock_send,
            patch("flows.agent_heartbeat.BackboneDB") as MockDB,
        ):
            mock_state.return_value = idle_snapshot
            mock_send.return_value = True

            # Mock DB context manager — first call for get_last_heartbeat, second for record
            db_instance = AsyncMock()
            db_instance.get_last_heartbeat = AsyncMock(return_value=None)
            db_instance.record_heartbeat = AsyncMock(return_value=1)
            MockDB.return_value.__aenter__ = AsyncMock(return_value=db_instance)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await evaluate_agent_heartbeat(
                agent="ike",
                schedule=schedule,
                active_sessions={"ike"},
                config=heartbeat_config,
            )

        assert result == "delivered"
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][1]
        assert "[via:heartbeat]" in msg
        assert "Check ROUTINE.md and HEARTBEAT.md." in msg

    async def test_skips_disabled_agent(self, heartbeat_config):
        """Disabled schedule returns skipped_disabled."""
        schedule = {"cron": "0 * * * *", "enabled": False}
        result = await evaluate_agent_heartbeat(
            agent="ike",
            schedule=schedule,
            active_sessions={"ike"},
            config=heartbeat_config,
        )
        assert result == "skipped_disabled"

    async def test_skips_busy_agent(self, heartbeat_config):
        """Due but agent is busy — skipped_busy."""
        schedule = {"cron": "0 * * * *", "timezone": TZ, "enabled": True}
        busy_snapshot = StateSnapshot(state=AgentState.BUSY, source="push")

        with (
            patch("flows.agent_heartbeat.get_agent_state", new_callable=AsyncMock) as mock_state,
            patch("flows.agent_heartbeat.BackboneDB") as MockDB,
        ):
            mock_state.return_value = busy_snapshot
            db_instance = AsyncMock()
            db_instance.get_last_heartbeat = AsyncMock(return_value=None)
            db_instance.record_heartbeat = AsyncMock(return_value=1)
            MockDB.return_value.__aenter__ = AsyncMock(return_value=db_instance)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await evaluate_agent_heartbeat(
                agent="ike",
                schedule=schedule,
                active_sessions={"ike"},
                config=heartbeat_config,
            )

        assert result == "skipped_busy"

    async def test_skips_offline_agent(self, heartbeat_config):
        """Due and idle but no tmux session — skipped_offline."""
        schedule = {"cron": "0 * * * *", "timezone": TZ, "enabled": True}
        idle_snapshot = StateSnapshot(state=AgentState.IDLE, source="push")

        with (
            patch("flows.agent_heartbeat.get_agent_state", new_callable=AsyncMock) as mock_state,
            patch("flows.agent_heartbeat.BackboneDB") as MockDB,
        ):
            mock_state.return_value = idle_snapshot
            db_instance = AsyncMock()
            db_instance.get_last_heartbeat = AsyncMock(return_value=None)
            db_instance.record_heartbeat = AsyncMock(return_value=1)
            MockDB.return_value.__aenter__ = AsyncMock(return_value=db_instance)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await evaluate_agent_heartbeat(
                agent="ike",
                schedule=schedule,
                active_sessions=set(),  # no sessions
                config=heartbeat_config,
            )

        assert result == "skipped_offline"

    async def test_not_due_skips(self, heartbeat_config):
        """Schedule not due — returns not_due."""
        schedule = {"cron": "0 * * * *", "timezone": TZ, "enabled": True}
        tz = ZoneInfo(TZ)
        recent = (datetime.now(tz) - timedelta(minutes=1)).isoformat()

        with patch("flows.agent_heartbeat.BackboneDB") as MockDB:
            db_instance = AsyncMock()
            db_instance.get_last_heartbeat = AsyncMock(return_value=recent)
            MockDB.return_value.__aenter__ = AsyncMock(return_value=db_instance)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await evaluate_agent_heartbeat(
                agent="ike",
                schedule=schedule,
                active_sessions={"ike"},
                config=heartbeat_config,
            )

        assert result == "not_due"


# --- heartbeat_scheduler flow ---


class TestHeartbeatSchedulerFlow:
    async def test_full_cycle(self, tmp_path):
        """End-to-end: schedule file with one due agent → delivered."""
        schedule_path = tmp_path / "schedules.json"
        schedules = {"ike": {"cron": "0 * * * *", "timezone": TZ, "enabled": True}}
        schedule_path.write_text(json.dumps(schedules))

        config = BackboneConfig(
            github_token="test",
            webhook_secret="test",
            heartbeat=HeartbeatConfig(
                schedule_file=str(schedule_path),
                default_timezone=TZ,
            ),
        )

        idle_snapshot = StateSnapshot(state=AgentState.IDLE, source="push")

        with (
            patch("flows.agent_heartbeat.BackboneConfig") as MockConfig,
            patch("flows.agent_heartbeat.list_sessions", new_callable=AsyncMock) as mock_list,
            patch("flows.agent_heartbeat.get_agent_state", new_callable=AsyncMock) as mock_state,
            patch("flows.agent_heartbeat.send_message", new_callable=AsyncMock) as mock_send,
            patch("flows.agent_heartbeat.BackboneDB") as MockDB,
        ):
            MockConfig.from_toml.return_value = config
            mock_list.return_value = ["ike", "feynman"]
            mock_state.return_value = idle_snapshot
            mock_send.return_value = True

            db_instance = AsyncMock()
            db_instance.get_last_heartbeat = AsyncMock(return_value=None)
            db_instance.record_heartbeat = AsyncMock(return_value=1)
            MockDB.return_value.__aenter__ = AsyncMock(return_value=db_instance)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await heartbeat_scheduler()

        assert result.get("ike") == "delivered"
        mock_send.assert_called_once()

    async def test_concurrent_run_skipped(self):
        """Lock prevents parallel execution."""
        # Manually acquire the lock
        await _heartbeat_lock.acquire()
        try:
            result = await heartbeat_scheduler()
            assert result == {"_skipped": "concurrent_run"}
        finally:
            _heartbeat_lock.release()

    async def test_one_shot_deleted_after_fire(self, tmp_path):
        """One-shot entry is removed from JSON after successful delivery."""
        schedule_path = tmp_path / "schedules.json"
        tz = ZoneInfo(TZ)
        past = (datetime.now(tz) - timedelta(hours=1)).isoformat()
        schedules = {
            "ike": {"cron": "0 * * * *", "timezone": TZ, "enabled": True},
            "temp-agent": {"at": past, "timezone": TZ, "enabled": True, "one_shot": True},
        }
        schedule_path.write_text(json.dumps(schedules))

        config = BackboneConfig(
            github_token="test",
            webhook_secret="test",
            heartbeat=HeartbeatConfig(
                schedule_file=str(schedule_path),
                default_timezone=TZ,
            ),
        )
        idle_snapshot = StateSnapshot(state=AgentState.IDLE, source="push")

        with (
            patch("flows.agent_heartbeat.BackboneConfig") as MockConfig,
            patch("flows.agent_heartbeat.list_sessions", new_callable=AsyncMock) as mock_list,
            patch("flows.agent_heartbeat.get_agent_state", new_callable=AsyncMock) as mock_state,
            patch("flows.agent_heartbeat.send_message", new_callable=AsyncMock) as mock_send,
            patch("flows.agent_heartbeat.BackboneDB") as MockDB,
        ):
            MockConfig.from_toml.return_value = config
            mock_list.return_value = ["ike", "temp-agent"]
            mock_state.return_value = idle_snapshot
            mock_send.return_value = True

            db_instance = AsyncMock()
            db_instance.get_last_heartbeat = AsyncMock(return_value=None)
            db_instance.record_heartbeat = AsyncMock(return_value=1)
            MockDB.return_value.__aenter__ = AsyncMock(return_value=db_instance)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await heartbeat_scheduler()

        assert result.get("temp-agent") == "delivered"

        # Verify one-shot was removed from file
        remaining = json.loads(schedule_path.read_text())
        assert "temp-agent" not in remaining
        assert "ike" in remaining

    async def test_empty_schedules(self, tmp_path):
        """Empty schedule file returns empty result."""
        schedule_path = tmp_path / "schedules.json"
        schedule_path.write_text("{}")

        config = BackboneConfig(
            github_token="test",
            webhook_secret="test",
            heartbeat=HeartbeatConfig(
                schedule_file=str(schedule_path),
                default_timezone=TZ,
            ),
        )

        with patch("flows.agent_heartbeat.BackboneConfig") as MockConfig:
            MockConfig.from_toml.return_value = config
            result = await heartbeat_scheduler()

        assert result == {}
