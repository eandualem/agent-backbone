"""Tests for src/session_bridge.py — composite intelligence, resolution, safe delivery."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

from src.agent_state import AgentState, StateSnapshot
from src.config import BackboneConfig, SessionBridgeConfig
from src.session_bridge import (
    SessionIntelligence,
    get_session_intelligence,
    list_sessions_full,
    resolve_entity_session,
    safe_deliver,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_IDLE_SNAP = StateSnapshot(state=AgentState.IDLE, source="push")
_BUSY_SNAP = StateSnapshot(state=AgentState.BUSY, source="push")
_PROCESSING_SNAP = StateSnapshot(state=AgentState.PROCESSING_ISSUE, source="push")
_PLAN_WAITING_SNAP = StateSnapshot(state=AgentState.PLAN_WAITING, source="push")
_UNKNOWN_SNAP = StateSnapshot(state=AgentState.UNKNOWN, source="default")


def _patch_list_sessions(sessions: list[str]):
    """Patch list_sessions in session_bridge to return given list."""
    return patch(
        "src.session_bridge.list_sessions",
        new_callable=AsyncMock,
        return_value=sessions,
    )


def _patch_query_format_vars(vars_dict: dict[str, str]):
    """Patch query_format_vars in session_bridge to return given dict."""
    return patch(
        "src.session_bridge.query_format_vars",
        new_callable=AsyncMock,
        return_value=vars_dict,
    )


def _patch_get_agent_state(snap: StateSnapshot):
    """Patch get_agent_state in session_bridge to return given snapshot."""
    return patch(
        "src.session_bridge.get_agent_state",
        new_callable=AsyncMock,
        return_value=snap,
    )


def _patch_send_message(success: bool = True):
    """Patch send_message in session_bridge to return given bool."""
    return patch(
        "src.session_bridge.send_message",
        new_callable=AsyncMock,
        return_value=success,
    )


def _patch_session_exists(exists: bool = True):
    """Patch session_exists in session_bridge to return a fixed boolean."""
    return patch(
        "src.session_bridge.session_exists",
        new_callable=AsyncMock,
        return_value=exists,
    )


def _patch_db():
    """Patch BackboneDB for enqueue verification.

    Usage:
        with _patch_db() as (mock_db_cls, mock_db):
            ...
            mock_db.enqueue_message.assert_called_once_with(...)
    """

    class _DBContext:
        def __init__(self):
            self.mock_db = AsyncMock()
            self.patcher = patch("src.persistence.BackboneDB")

        def __enter__(self):
            mock_db_cls = self.patcher.__enter__()
            mock_db_cls.return_value.__aenter__ = AsyncMock(return_value=self.mock_db)
            mock_db_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            return mock_db_cls, self.mock_db

        def __exit__(self, *exc):
            return self.patcher.__exit__(*exc)

    return _DBContext()


def _default_config() -> BackboneConfig:
    """BackboneConfig with defaults (no TOML, no env vars needed)."""
    return BackboneConfig(github_token="test-token", webhook_secret="test-secret")


def _config_with_grace(seconds: int) -> BackboneConfig:
    """BackboneConfig with a specific grace_period_seconds."""
    return BackboneConfig(
        github_token="test-token",
        webhook_secret="test-secret",
        session_bridge=SessionBridgeConfig(grace_period_seconds=seconds),
    )


# ---------------------------------------------------------------------------
# TestGetSessionIntelligence
# ---------------------------------------------------------------------------


class TestGetSessionIntelligence:
    """Tests for get_session_intelligence() — priority-ordered derivation."""

    async def test_offline_when_session_not_active(self):
        """Session not in list_sessions returns OFFLINE."""
        config = _default_config()
        with _patch_list_sessions([]):
            profile = await get_session_intelligence("ike", config)

        assert profile.intelligence == SessionIntelligence.OFFLINE
        assert profile.session_name == "ike"
        assert profile.agent_state == AgentState.UNKNOWN

    async def test_copy_mode(self):
        """pane_in_mode=1 returns COPY_MODE (priority 2)."""
        config = _default_config()
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "1", "client_activity": "0"}),
            _patch_get_agent_state(_IDLE_SNAP),
        ):
            profile = await get_session_intelligence("ike", config)

        assert profile.intelligence == SessionIntelligence.COPY_MODE
        assert profile.agent_state == AgentState.IDLE
        assert profile.tmux_vars["pane_in_mode"] == "1"

    async def test_user_interacting(self):
        """Recent client_activity + agent IDLE returns USER_INTERACTING (priority 3)."""
        config = _default_config()
        recent_activity = str(time.time() - 2)  # 2 seconds ago (within 10s threshold)
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": recent_activity}),
            _patch_get_agent_state(_IDLE_SNAP),
        ):
            profile = await get_session_intelligence("ike", config)

        assert profile.intelligence == SessionIntelligence.USER_INTERACTING

    async def test_plan_waiting(self):
        """Agent state PLAN_WAITING returns PLAN_WAITING (priority 4)."""
        config = _default_config()
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_PLAN_WAITING_SNAP),
        ):
            profile = await get_session_intelligence("ike", config)

        assert profile.intelligence == SessionIntelligence.PLAN_WAITING
        assert profile.agent_state == AgentState.PLAN_WAITING

    async def test_agent_working_busy(self):
        """Agent state BUSY returns AGENT_WORKING (priority 5)."""
        config = _default_config()
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_BUSY_SNAP),
        ):
            profile = await get_session_intelligence("ike", config)

        assert profile.intelligence == SessionIntelligence.AGENT_WORKING
        assert profile.agent_state == AgentState.BUSY

    async def test_agent_working_processing(self):
        """Agent state PROCESSING_ISSUE returns AGENT_WORKING (priority 5)."""
        config = _default_config()
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_PROCESSING_SNAP),
        ):
            profile = await get_session_intelligence("ike", config)

        assert profile.intelligence == SessionIntelligence.AGENT_WORKING
        assert profile.agent_state == AgentState.PROCESSING_ISSUE

    async def test_idle_ready(self):
        """Agent IDLE with no idle_since returns IDLE_READY (priority 7)."""
        config = _default_config()
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_IDLE_SNAP),
        ):
            # idle_since=None skips grace check
            profile = await get_session_intelligence("ike", config, idle_since=None)

        assert profile.intelligence == SessionIntelligence.IDLE_READY
        assert profile.agent_state == AgentState.IDLE

    async def test_idle_grace(self):
        """Agent IDLE with grace not yet elapsed returns IDLE_GRACE (priority 6)."""
        config = _config_with_grace(5)
        # idle_since is "just now" in monotonic time, so grace period not elapsed
        idle_since = time.monotonic() - 1  # 1 second ago, grace is 5 seconds
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_IDLE_SNAP),
        ):
            profile = await get_session_intelligence("ike", config, idle_since=idle_since)

        assert profile.intelligence == SessionIntelligence.IDLE_GRACE
        assert profile.agent_state == AgentState.IDLE

    async def test_unknown_state(self):
        """Agent state UNKNOWN returns UNKNOWN (priority 8)."""
        config = _default_config()
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_UNKNOWN_SNAP),
        ):
            profile = await get_session_intelligence("ike", config)

        assert profile.intelligence == SessionIntelligence.UNKNOWN
        assert profile.agent_state == AgentState.UNKNOWN


# ---------------------------------------------------------------------------
# TestResolveEntitySession
# ---------------------------------------------------------------------------


class TestResolveEntitySession:
    """Tests for resolve_entity_session() — entity-to-session mapping."""

    async def test_named_entity(self):
        """Named entity 'ike' maps directly to 'ike' session."""
        config = _default_config()
        result = await resolve_entity_session("ike", config)
        assert result == "ike"

    async def test_skip_set(self):
        """Entity in skip set ('elias') returns None."""
        config = _default_config()
        result = await resolve_entity_session("elias", config)
        assert result is None

    async def test_coding_agent_with_repo_session(self):
        """coding-agent extracts repo from title and finds active session."""
        config = _default_config()
        with _patch_session_exists(True):
            result = await resolve_entity_session(
                "coding-agent",
                config,
                issue_title="[task] platform-api: Fix auth timeout",
            )
        assert result == "platform-api"

    async def test_coding_agent_no_session_fallback(self):
        """coding-agent with no matching session falls back to 'ike'."""
        config = _default_config()
        with _patch_session_exists(False):
            result = await resolve_entity_session(
                "coding-agent",
                config,
                issue_title="[task] platform-api: Fix auth timeout",
            )
        assert result == "ike"

    async def test_coding_agent_no_title_extraction(self):
        """use_title_extraction=False skips title parsing, goes to fallback."""
        config = _default_config()
        result = await resolve_entity_session(
            "coding-agent",
            config,
            issue_title="[task] platform-api: Fix auth timeout",
            use_title_extraction=False,
        )
        assert result == "ike"

    async def test_coding_agent_no_repo_in_title(self):
        """Title without matching repo pattern falls back to 'ike'."""
        config = _default_config()
        result = await resolve_entity_session(
            "coding-agent",
            config,
            issue_title="Some random title without brackets",
        )
        assert result == "ike"

    async def test_unknown_entity(self):
        """Unknown entity 'nobody' returns None."""
        config = _default_config()
        result = await resolve_entity_session("nobody", config)
        assert result is None


# ---------------------------------------------------------------------------
# TestSafeDeliver
# ---------------------------------------------------------------------------


class TestSafeDeliver:
    """Tests for safe_deliver() — state-aware delivery with queuing."""

    async def test_delivered_idle_ready(self):
        """IDLE_READY + send succeeds returns 'delivered'."""
        config = _default_config()
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_IDLE_SNAP),
            _patch_send_message(True) as mock_send,
        ):
            result = await safe_deliver("ike", "Hello", config)

        assert result == "delivered"
        mock_send.assert_called_once_with("ike", "Hello")

    async def test_offline_enqueues(self):
        """OFFLINE with issue_number + target_entity enqueues to DB."""
        config = _default_config()
        with (
            _patch_list_sessions([]),
            _patch_db() as (_, mock_db),
        ):
            result = await safe_deliver(
                "ike",
                "Hello",
                config,
                issue_number=42,
                target_entity="ike",
                flow_name="test_flow",
            )

        assert result == "offline"
        mock_db.enqueue_message.assert_called_once_with(
            session_name="ike",
            message="Hello",
            issue_number=42,
            target_entity="ike",
            flow_name="test_flow",
        )

    async def test_offline_no_enqueue_without_issue_number(self):
        """OFFLINE without issue_number does not enqueue."""
        config = _default_config()
        with (
            _patch_list_sessions([]),
            _patch_db() as (_, mock_db),
        ):
            result = await safe_deliver("ike", "Hello", config)

        assert result == "offline"
        mock_db.enqueue_message.assert_not_called()

    async def test_copy_mode_blocks(self):
        """COPY_MODE without priority returns 'copy_mode'."""
        config = _default_config()
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "1", "client_activity": "0"}),
            _patch_get_agent_state(_IDLE_SNAP),
        ):
            result = await safe_deliver("ike", "Hello", config, priority=False)

        assert result == "copy_mode"

    async def test_copy_mode_priority_bypasses(self):
        """COPY_MODE + priority=True bypasses and delivers."""
        config = _default_config()
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "1", "client_activity": "0"}),
            _patch_get_agent_state(_IDLE_SNAP),
            _patch_send_message(True),
        ):
            result = await safe_deliver("ike", "Hello", config, priority=True)

        assert result == "delivered"

    async def test_user_interacting_blocks(self):
        """USER_INTERACTING without priority returns 'user_interacting'."""
        config = _default_config()
        recent_activity = str(time.time() - 2)
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": recent_activity}),
            _patch_get_agent_state(_IDLE_SNAP),
        ):
            result = await safe_deliver("ike", "Hello", config, priority=False)

        assert result == "user_interacting"

    async def test_agent_working_blocks(self):
        """AGENT_WORKING returns 'agent_working'."""
        config = _default_config()
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_BUSY_SNAP),
        ):
            result = await safe_deliver("ike", "Hello", config)

        assert result == "agent_working"

    async def test_plan_waiting_blocks_even_priority(self):
        """PLAN_WAITING blocks delivery even with priority=True."""
        config = _default_config()
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_PLAN_WAITING_SNAP),
        ):
            result = await safe_deliver("ike", "Hello", config, priority=True)

        assert result == "plan_waiting"

    async def test_delivery_failed_enqueues(self):
        """IDLE_READY + send fails returns 'delivery_failed' and enqueues."""
        config = _default_config()
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_IDLE_SNAP),
            _patch_send_message(False),
            _patch_db() as (_, mock_db),
        ):
            result = await safe_deliver(
                "ike",
                "Hello",
                config,
                issue_number=42,
                target_entity="ike",
                flow_name="test_flow",
            )

        assert result == "delivery_failed"
        mock_db.enqueue_message.assert_called_once_with(
            session_name="ike",
            message="Hello",
            issue_number=42,
            target_entity="ike",
            flow_name="test_flow",
        )

    async def test_unknown_delivers(self):
        """UNKNOWN state still attempts delivery."""
        config = _default_config()
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_UNKNOWN_SNAP),
            _patch_send_message(True) as mock_send,
        ):
            result = await safe_deliver("ike", "Hello", config)

        assert result == "delivered"
        mock_send.assert_called_once_with("ike", "Hello")


# ---------------------------------------------------------------------------
# TestListSessionsFull
# ---------------------------------------------------------------------------


class TestListSessionsFull:
    """Tests for list_sessions_full() — enriched session listing."""

    async def test_enriches_sessions(self):
        """Returns sessions with intelligence and agent_state fields."""
        config = _default_config()
        mock_sessions = [
            {"name": "ike", "windows": 1, "created": 1000, "attached": True},
            {"name": "leo", "windows": 2, "created": 2000, "attached": False},
        ]
        with (
            patch(
                "src.tmux.list_sessions_rich",
                new_callable=AsyncMock,
                return_value=mock_sessions,
            ),
            _patch_list_sessions(["ike", "leo"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_IDLE_SNAP),
        ):
            result = await list_sessions_full(config)

        assert len(result) == 2
        assert result[0]["name"] == "ike"
        assert result[0]["intelligence"] == "idle_ready"
        assert result[0]["agent_state"] == "idle"
        assert result[0]["windows"] == 1
        assert result[0]["attached"] is True
        assert result[1]["name"] == "leo"
        assert result[1]["intelligence"] == "idle_ready"
        assert result[1]["agent_state"] == "idle"
        assert result[1]["windows"] == 2

    async def test_empty_list(self):
        """Returns empty list when no sessions exist."""
        config = _default_config()
        with patch("src.tmux.list_sessions_rich", new_callable=AsyncMock, return_value=[]):
            result = await list_sessions_full(config)
        assert result == []

    async def test_correct_intelligence_values(self):
        """Intelligence values reflect actual session states (e.g. copy_mode)."""
        config = _default_config()
        mock_sessions = [
            {"name": "ike", "windows": 1, "created": 1000, "attached": True},
        ]
        with (
            patch(
                "src.tmux.list_sessions_rich",
                new_callable=AsyncMock,
                return_value=mock_sessions,
            ),
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "1", "client_activity": "0"}),
            _patch_get_agent_state(_IDLE_SNAP),
        ):
            result = await list_sessions_full(config)

        assert len(result) == 1
        assert result[0]["intelligence"] == "copy_mode"
        assert result[0]["agent_state"] == "idle"

    async def test_preserves_rich_fields(self):
        """All fields from list_sessions_rich are preserved in the output."""
        config = _default_config()
        mock_sessions = [
            {"name": "ada", "windows": 3, "created": 5000, "attached": False},
        ]
        with (
            patch(
                "src.tmux.list_sessions_rich",
                new_callable=AsyncMock,
                return_value=mock_sessions,
            ),
            _patch_list_sessions(["ada"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_UNKNOWN_SNAP),
        ):
            result = await list_sessions_full(config)

        assert len(result) == 1
        assert result[0]["name"] == "ada"
        assert result[0]["windows"] == 3
        assert result[0]["created"] == 5000
        assert result[0]["attached"] is False
        assert result[0]["intelligence"] == "unknown"
        assert result[0]["agent_state"] == "unknown"
