"""Tests for agent_backbone/services/delivery — resolution and safe delivery."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.config import BackboneConfig, JarvisConfig, SessionBridgeConfig
from agent_backbone.services.agents import AgentState, StateSnapshot
from agent_backbone.services.registry import EntityEntry, EntityRegistry, RepoInfo
from agent_backbone.services.routing import (
    SessionIntelligence,
    SessionProfile,
    get_session_intelligence,
    list_sessions_full,
    resolve_entity_session,
    resolve_entity_sessions,
    safe_deliver,
)
from agent_backbone.services.routing._resolution import validate_issue_targets

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_IDLE_SNAP = StateSnapshot(state=AgentState.IDLE, source="push")
_BUSY_SNAP = StateSnapshot(state=AgentState.BUSY, source="push")
_PROCESSING_SNAP = StateSnapshot(state=AgentState.PROCESSING_ISSUE, source="push")
_PLAN_WAITING_SNAP = StateSnapshot(state=AgentState.PLAN_WAITING, source="push")
_PERMISSION_WAITING_SNAP = StateSnapshot(state=AgentState.PERMISSION_WAITING, source="push")
_UNKNOWN_SNAP = StateSnapshot(state=AgentState.UNKNOWN, source="default")
_PROCESSING_ISSUE_42_SNAP = StateSnapshot(
    state=AgentState.PROCESSING_ISSUE,
    current_issue=42,
    source="push",
)
_PROCESSING_ISSUE_99_SNAP = StateSnapshot(
    state=AgentState.PROCESSING_ISSUE,
    current_issue=99,
    source="push",
)
_PLAN_WAITING_ISSUE_42_SNAP = StateSnapshot(
    state=AgentState.PLAN_WAITING,
    current_issue=42,
    source="push",
)
_PERMISSION_WAITING_ISSUE_42_SNAP = StateSnapshot(
    state=AgentState.PERMISSION_WAITING,
    current_issue=42,
    source="push",
)


@pytest.fixture(autouse=True)
def _patch_tmux_runtime_env():
    """Keep runtime resolution inside unit tests by disabling tmux env lookups."""
    with patch(
        "agent_backbone.services.terminal._adapters.query_environment_var",
        new_callable=AsyncMock,
        return_value=None,
    ):
        yield


@pytest.fixture(autouse=True)
def _patch_default_capture_pane():
    """Default pane capture is empty unless a test provides a specific prompt surface."""
    with patch(
        "agent_backbone.services.routing._intelligence.capture_pane",
        new_callable=AsyncMock,
        return_value="",
    ):
        yield


def _patch_list_sessions(sessions: list[str]):
    """Patch list_sessions in session_bridge to return given list."""
    return patch(
        "agent_backbone.services.routing._intelligence.list_sessions",
        new_callable=AsyncMock,
        return_value=sessions,
    )


def _patch_query_format_vars(vars_dict: dict[str, str]):
    """Patch query_format_vars in session_bridge to return given dict."""
    return patch(
        "agent_backbone.services.routing._intelligence.query_format_vars",
        new_callable=AsyncMock,
        return_value=vars_dict,
    )


def _patch_get_agent_state(snap: StateSnapshot):
    """Patch get_agent_state in session_bridge to return given snapshot."""
    return patch(
        "agent_backbone.services.routing._intelligence.get_agent_state",
        new_callable=AsyncMock,
        return_value=snap,
    )


def _patch_capture_pane(content: str):
    """Patch capture_pane in session_bridge to return pane content."""
    return patch(
        "agent_backbone.services.routing._intelligence.capture_pane",
        new_callable=AsyncMock,
        return_value=content,
    )


def _patch_send_message(success: bool = True):
    """Patch send_message in session_bridge to return given bool."""
    return patch(
        "agent_backbone.services.routing._delivery.send_message",
        new_callable=AsyncMock,
        return_value=success,
    )


def _patch_session_exists(exists: bool = True):
    """Patch session_exists in session_bridge to return a fixed boolean."""
    return patch(
        "agent_backbone.services.routing._resolution.session_exists",
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
            self.patcher = patch("agent_backbone.services.database.BackboneDB")

        def __enter__(self):
            mock_db_cls = self.patcher.__enter__()
            mock_db_cls.return_value.__aenter__ = AsyncMock(return_value=self.mock_db)
            mock_db_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            return mock_db_cls, self.mock_db

        def __exit__(self, *exc):
            return self.patcher.__exit__(*exc)

    return _DBContext()


def _test_registry() -> EntityRegistry:
    """Registry with standard test entities."""
    return EntityRegistry(
        entities={
            "ike": EntityEntry(
                session="ike",
                home="~/ws/core/ike",
                groups=[],
                figure="",
                role="Core Orchestrator",
            ),
            "feynman": EntityEntry(
                session="feynman",
                home="~/orchestration",
                groups=[],
                figure="",
                role="Orchestration Optimizer",
            ),
            "leo": EntityEntry(
                session="leo",
                home="~/ws/leo",
                groups=[],
                figure="",
                role="Strategy Co-Architect",
            ),
            "ada": EntityEntry(
                session="ada",
                home="~/ws/core/spec",
                groups=[],
                figure="",
                role="Spec Agent",
            ),
        },
        repos=[],
    )


def _test_registry_with_repos() -> EntityRegistry:
    """Registry with standard test entities and repos."""
    return EntityRegistry(
        entities={
            "ike": EntityEntry(
                session="ike",
                home="~/ws/core/ike",
                groups=[],
                figure="",
                role="Core Orchestrator",
            ),
        },
        repos=[
            RepoInfo(org="WF", name="agent-backbone", path="/ws/core/code/WF/agent-backbone"),
        ],
    )


def _default_config() -> BackboneConfig:
    """BackboneConfig with defaults (no TOML, no env vars needed)."""
    return BackboneConfig(
        webhook_secret="test-secret",
        registry=_test_registry(),
    )


def _config_with_grace(seconds: int) -> BackboneConfig:
    """BackboneConfig with a specific grace_period_seconds."""
    return BackboneConfig(
        webhook_secret="test-secret",
        registry=_test_registry(),
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

    async def test_recent_client_activity_does_not_trigger_user_interacting(self):
        """Attached-or-reading tmux activity alone must not block delivery."""
        config = _default_config()
        recent_activity = str(time.time() - 2)
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": recent_activity}),
            _patch_get_agent_state(_IDLE_SNAP),
            _patch_capture_pane(""),
        ):
            profile = await get_session_intelligence("ike", config)

        assert profile.intelligence == SessionIntelligence.IDLE_READY

    async def test_user_interacting_when_prompt_has_buffered_input(self):
        """Buffered Codex input counts as active terminal interaction."""
        config = _default_config()
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_IDLE_SNAP),
            _patch_capture_pane("\u203a Review the routing fallback logic"),
        ):
            profile = await get_session_intelligence("ike", config)

        assert profile.intelligence == SessionIntelligence.USER_INTERACTING

    async def test_user_interacting_when_codex_status_line_is_below_prompt(self):
        """Codex status chrome below the prompt should not hide pending input."""
        config = _default_config()
        pane = (
            "\u203a Review the routing fallback logic\n\n"
            "  gpt-5.4 xhigh \u00b7 91% left \u00b7 ~/ws/core/code/WF/agent-orchestration-dashboard"
        )
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_IDLE_SNAP),
            _patch_capture_pane(pane),
        ):
            profile = await get_session_intelligence("ike", config)

        assert profile.intelligence == SessionIntelligence.USER_INTERACTING

    async def test_codex_placeholder_prompt_is_idle_ready(self):
        """Codex placeholder chrome should not be mistaken for buffered input."""
        config = _default_config()
        pane = "\x1b[1m\u203a\x1b[0m \x1b[2mImprove documentation in @filename\x1b[0m"
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_IDLE_SNAP),
            _patch_capture_pane(pane),
        ):
            profile = await get_session_intelligence("ike", config)

        assert profile.intelligence == SessionIntelligence.IDLE_READY

    async def test_codex_placeholder_with_status_line_is_idle_ready(self):
        """Codex placeholder plus status chrome should remain idle."""
        config = _default_config()
        pane = (
            "\x1b[1m\u203a\x1b[0m \x1b[2mImprove documentation in @filename\x1b[0m\n\n"
            "  gpt-5.4 xhigh \u00b7 91% left \u00b7 ~/ws/core/code/WF/agent-orchestration-dashboard"
        )
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_IDLE_SNAP),
            _patch_capture_pane(pane),
        ):
            profile = await get_session_intelligence("ike", config)

        assert profile.intelligence == SessionIntelligence.IDLE_READY

    async def test_plan_waiting(self):
        """Agent state PLAN_WAITING returns PLAN_WAITING before tmux-only signals."""
        config = _default_config()
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_PLAN_WAITING_SNAP),
        ):
            profile = await get_session_intelligence("ike", config)

        assert profile.intelligence == SessionIntelligence.PLAN_WAITING
        assert profile.agent_state == AgentState.PLAN_WAITING

    async def test_permission_waiting(self):
        """Agent state PERMISSION_WAITING returns PERMISSION_WAITING."""
        config = _default_config()
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_PERMISSION_WAITING_SNAP),
        ):
            profile = await get_session_intelligence("ike", config)

        assert profile.intelligence == SessionIntelligence.PERMISSION_WAITING
        assert profile.agent_state == AgentState.PERMISSION_WAITING

    async def test_copy_mode_does_not_mask_plan_waiting(self):
        """PLAN_WAITING must outrank pane_in_mode copy-mode signals."""
        config = _default_config()
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "1", "client_activity": "0"}),
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

    async def test_busy_profile_drops_stale_current_issue(self):
        """BUSY snapshots should not expose a current_issue in the session profile."""
        config = _default_config()
        busy_with_issue = StateSnapshot(state=AgentState.BUSY, current_issue=42, source="push")
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(busy_with_issue),
        ):
            profile = await get_session_intelligence("ike", config)

        assert profile.intelligence == SessionIntelligence.AGENT_WORKING
        assert profile.current_issue is None

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

    async def test_copy_mode_does_not_mask_agent_working_busy(self):
        """Bug #620: BUSY agent in copy mode must resolve AGENT_WORKING, not COPY_MODE."""
        config = _default_config()
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "1", "client_activity": "0"}),
            _patch_get_agent_state(_BUSY_SNAP),
        ):
            profile = await get_session_intelligence("ike", config)

        assert profile.intelligence == SessionIntelligence.AGENT_WORKING
        assert profile.agent_state == AgentState.BUSY

    async def test_copy_mode_does_not_mask_agent_working_processing(self):
        """Bug #620: PROCESSING_ISSUE agent in copy mode must resolve AGENT_WORKING."""
        config = _default_config()
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "1", "client_activity": "0"}),
            _patch_get_agent_state(_PROCESSING_SNAP),
        ):
            profile = await get_session_intelligence("ike", config)

        assert profile.intelligence == SessionIntelligence.AGENT_WORKING
        assert profile.agent_state == AgentState.PROCESSING_ISSUE


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

    async def test_validate_issue_targets_rejects_abstract_shared_roles(self):
        """New issue creation must reject abstract shared-role aliases."""
        config = BackboneConfig(
            webhook_secret="test-secret",
            registry=EntityRegistry(
                entities={
                    "bell-wf": EntityEntry(
                        session="bell-wf",
                        home="~/ws/core/code/WF/bell",
                        groups=["orchestrators"],
                        figure="",
                        role="Org Orchestrator",
                        organization="WF",
                        entity_type="role-instance",
                    ),
                    "bell-loveble": EntityEntry(
                        session="bell-loveble",
                        home="~/ws/core/code/Loveble/bell",
                        groups=["orchestrators"],
                        figure="",
                        role="Org Orchestrator",
                        organization="Loveble",
                        entity_type="role-instance",
                    ),
                },
                repos=[],
            ),
        )

        with pytest.raises(ValueError, match="invalid issue target 'bell'"):
            validate_issue_targets(["bell"], config)

    async def test_concrete_role_instance_target_resolves_directly(self):
        """Concrete role-instance targets resolve like any other named entity."""
        config = BackboneConfig(
            webhook_secret="test-secret",
            registry=EntityRegistry(
                entities={
                    "bell-wf": EntityEntry(
                        session="bell-wf",
                        home="~/ws/core/code/WF/bell",
                        groups=["orchestrators"],
                        figure="",
                        role="Org Orchestrator",
                        organization="WF",
                        entity_type="role-instance",
                    ),
                },
                repos=[],
            ),
        )

        result = await resolve_entity_sessions("bell-wf", config)

        assert result == ["bell-wf"]

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

    async def test_repo_name_with_active_session(self):
        """Known repo name with active tmux session resolves to that session."""
        config = BackboneConfig(
            webhook_secret="test-secret",
            registry=_test_registry_with_repos(),
        )
        with _patch_session_exists(True):
            result = await resolve_entity_session("agent-backbone", config)
        assert result == "agent-backbone"

    async def test_repo_name_session_not_running(self):
        """Known repo name without active tmux session returns None."""
        config = BackboneConfig(
            webhook_secret="test-secret",
            registry=_test_registry_with_repos(),
        )
        with _patch_session_exists(False):
            result = await resolve_entity_session("agent-backbone", config)
        assert result is None

    async def test_unknown_entity(self):
        """Unknown entity 'nobody' returns None."""
        config = _default_config()
        result = await resolve_entity_session("nobody", config)
        assert result is None

    async def test_jarvis_enabled(self):
        """Jarvis resolves to 'jarvis' when inject_url is configured."""
        config = BackboneConfig(
            webhook_secret="s",
            jarvis=JarvisConfig(inject_url="http://localhost:3000/api/assistant/inject"),
        )
        result = await resolve_entity_session("jarvis", config)
        assert result == "jarvis"

    async def test_jarvis_disabled(self):
        """Jarvis returns None when inject_url is not set."""
        config = _default_config()
        result = await resolve_entity_session("jarvis", config)
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
        mock_send.assert_called_once_with("ike", "Hello", runtime_hint="unknown")

    async def test_offline_enqueues(self):
        """OFFLINE with issue_number + target_entity enqueues to DB."""
        config = _default_config()
        mock_db = AsyncMock()
        with _patch_list_sessions([]):
            result = await safe_deliver(
                "ike",
                "Hello",
                config,
                db=mock_db,
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
            delivery_kind="issue",
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

    async def test_copy_mode_recovers_and_delivers(self):
        """COPY_MODE triggers recovery and delivers when tmux leaves copy mode."""
        config = _default_config()
        copy_profile = SessionProfile(
            session_name="ike",
            intelligence=SessionIntelligence.COPY_MODE,
            runtime="codex",
            agent_state=AgentState.IDLE,
        )
        idle_profile = SessionProfile(
            session_name="ike",
            intelligence=SessionIntelligence.IDLE_READY,
            runtime="codex",
            agent_state=AgentState.IDLE,
        )
        adapter = AsyncMock()
        adapter.exit_copy_mode.return_value = True

        with (
            patch(
                "agent_backbone.services.routing._delivery.get_session_intelligence",
                new_callable=AsyncMock,
                side_effect=[copy_profile, idle_profile],
            ),
            patch(
                "agent_backbone.services.routing._delivery.get_terminal_adapter",
                return_value=adapter,
            ),
            _patch_send_message(True),
        ):
            result = await safe_deliver("ike", "Hello", config, priority=False)

        assert result == "delivered"
        adapter.exit_copy_mode.assert_awaited_once_with("ike")

    async def test_copy_mode_priority_bypasses(self):
        """Priority delivery still recovers copy mode before delivering."""
        config = _default_config()
        copy_profile = SessionProfile(
            session_name="ike",
            intelligence=SessionIntelligence.COPY_MODE,
            runtime="codex",
            agent_state=AgentState.IDLE,
        )
        idle_profile = SessionProfile(
            session_name="ike",
            intelligence=SessionIntelligence.IDLE_READY,
            runtime="codex",
            agent_state=AgentState.IDLE,
        )
        adapter = AsyncMock()
        adapter.exit_copy_mode.return_value = True

        with (
            patch(
                "agent_backbone.services.routing._delivery.get_session_intelligence",
                new_callable=AsyncMock,
                side_effect=[copy_profile, idle_profile],
            ),
            patch(
                "agent_backbone.services.routing._delivery.get_terminal_adapter",
                return_value=adapter,
            ),
            _patch_send_message(True),
        ):
            result = await safe_deliver("ike", "Hello", config, priority=True)

        assert result == "delivered"
        adapter.exit_copy_mode.assert_awaited_once_with("ike")

    async def test_user_interacting_blocks(self):
        """Buffered prompt input without priority returns 'user_interacting'."""
        config = _default_config()
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_IDLE_SNAP),
            _patch_capture_pane("\u203a Review the fallback routing logic"),
        ):
            result = await safe_deliver("ike", "Hello", config, priority=False)

        assert result == "user_interacting"

    async def test_recent_client_activity_alone_does_not_block(self):
        """Recent tmux activity with an empty prompt should still deliver."""
        config = _default_config()
        recent_activity = str(time.time() - 2)
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": recent_activity}),
            _patch_get_agent_state(_IDLE_SNAP),
            _patch_capture_pane(""),
            _patch_send_message(True),
        ):
            result = await safe_deliver("ike", "Hello", config, priority=False)

        assert result == "delivered"

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

    async def test_direct_message_defers_durably_while_agent_working(self):
        """Direct messages should queue even without issue metadata."""
        config = _default_config()
        mock_db = AsyncMock()
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_BUSY_SNAP),
        ):
            result = await safe_deliver(
                "ike",
                "Hello",
                config,
                db=mock_db,
                flow_name="api-messages",
                delivery_kind="direct_message",
            )

        assert result == "agent_working"
        mock_db.enqueue_message.assert_called_once_with(
            session_name="ike",
            message="Hello",
            issue_number=None,
            target_entity=None,
            delivery_kind="direct_message",
            flow_name="api-messages",
        )

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

    async def test_permission_waiting_blocks_even_priority(self):
        """PERMISSION_WAITING blocks delivery even with priority=True."""
        config = _default_config()
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_PERMISSION_WAITING_SNAP),
        ):
            result = await safe_deliver("ike", "Hello", config, priority=True)

        assert result == "permission_waiting"

    async def test_persistent_copy_mode_fails_and_enqueues(self):
        """COPY_MODE that persists after recovery is queued as a normal failure."""
        config = _default_config()
        mock_db = AsyncMock()
        copy_profile = SessionProfile(
            session_name="ike",
            intelligence=SessionIntelligence.COPY_MODE,
            runtime="codex",
            agent_state=AgentState.IDLE,
        )
        adapter = AsyncMock()
        adapter.exit_copy_mode.return_value = True

        with (
            patch(
                "agent_backbone.services.routing._delivery.get_session_intelligence",
                new_callable=AsyncMock,
                side_effect=[copy_profile, copy_profile],
            ),
            patch(
                "agent_backbone.services.routing._delivery.get_terminal_adapter",
                return_value=adapter,
            ),
        ):
            result = await safe_deliver(
                "ike",
                "Hello",
                config,
                db=mock_db,
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
            delivery_kind="issue",
            flow_name="test_flow",
        )
        mock_db.record_delivery.assert_called_once_with(
            issue_number=42,
            target_entity="ike",
            session_name="ike",
            outcome="delivery_failed",
            flow_name="test_flow",
        )

    async def test_user_interacting_enqueues(self):
        """Buffered prompt input enqueues message to DB when tracking info provided."""
        config = _default_config()
        mock_db = AsyncMock()
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_IDLE_SNAP),
            _patch_capture_pane("\u203a Review the fallback routing logic"),
        ):
            result = await safe_deliver(
                "ike",
                "Hello",
                config,
                db=mock_db,
                issue_number=42,
                target_entity="ike",
                flow_name="test_flow",
            )

        assert result == "user_interacting"
        mock_db.enqueue_message.assert_called_once_with(
            session_name="ike",
            message="Hello",
            issue_number=42,
            target_entity="ike",
            delivery_kind="issue",
            flow_name="test_flow",
        )

    async def test_delivery_failed_enqueues(self):
        """IDLE_READY + send fails returns 'delivery_failed' and enqueues."""
        config = _default_config()
        mock_db = AsyncMock()
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_IDLE_SNAP),
            _patch_send_message(False),
        ):
            result = await safe_deliver(
                "ike",
                "Hello",
                config,
                db=mock_db,
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
            delivery_kind="issue",
            flow_name="test_flow",
        )

    async def test_unknown_state_still_delivers(self):
        """UNKNOWN is a deliverable fallback under the hardened spec."""
        config = _default_config()
        mock_db = AsyncMock()
        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_UNKNOWN_SNAP),
            _patch_send_message(True),
        ):
            result = await safe_deliver(
                "ike",
                "Hello",
                config,
                db=mock_db,
                issue_number=42,
                target_entity="ike",
                flow_name="test_flow",
            )

        assert result == "delivered"
        mock_db.enqueue_message.assert_not_called()
        mock_db.record_delivery.assert_called_once_with(
            issue_number=42,
            target_entity="ike",
            session_name="ike",
            outcome="delivered",
            flow_name="test_flow",
        )

    async def test_enforce_issue_queue_blocks_duplicate_issue(self):
        """Issue queue enforcement suppresses the same issue after first delivery."""
        config = _default_config()
        mock_db = AsyncMock()
        mock_db.query_deliveries.return_value = [
            {
                "issue_number": 42,
                "target_entity": "ike",
                "session_name": "ike",
                "outcome": "delivered",
            }
        ]

        result = await safe_deliver(
            "ike",
            "Hello",
            config,
            db=mock_db,
            issue_number=42,
            target_entity="ike",
            flow_name="test_flow",
            enforce_issue_queue=True,
        )

        assert result == "already_delivered"
        mock_db.record_delivery.assert_not_called()

    async def test_enforce_issue_queue_blocks_until_acknowledged(self):
        """A newer issue is blocked while the last delivered one is still unacked."""
        config = _default_config()
        mock_db = AsyncMock()
        mock_db.query_deliveries.side_effect = [
            [],
            [
                {
                    "issue_number": 41,
                    "target_entity": "ike",
                    "session_name": "ike",
                    "outcome": "delivered",
                }
            ],
        ]
        mock_db.is_acknowledged.return_value = False

        result = await safe_deliver(
            "ike",
            "Hello",
            config,
            db=mock_db,
            issue_number=42,
            target_entity="ike",
            flow_name="test_flow",
            enforce_issue_queue=True,
        )

        assert result == "awaiting_ack"
        mock_db.record_delivery.assert_not_called()

    async def test_enforce_issue_queue_records_delivery_attempt(self):
        """Central queue enforcement records the successful delivery attempt."""
        config = _default_config()
        mock_db = AsyncMock()
        mock_db.query_deliveries.side_effect = [[], []]

        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_IDLE_SNAP),
            _patch_send_message(True),
        ):
            result = await safe_deliver(
                "ike",
                "Hello",
                config,
                db=mock_db,
                issue_number=42,
                target_entity="ike",
                flow_name="test_flow",
                enforce_issue_queue=True,
            )

        assert result == "delivered"
        mock_db.record_delivery.assert_called_once_with(
            issue_number=42,
            target_entity="ike",
            session_name="ike",
            outcome="delivered",
            flow_name="test_flow",
        )

    async def test_enforce_issue_queue_ignores_stale_delivery_outside_open_queue(self):
        """Closed or unrelated historical deliveries must not block the current queue."""
        config = _default_config()
        mock_db = AsyncMock()
        mock_db.query_deliveries.side_effect = [
            [],
            [
                {
                    "issue_number": 665,
                    "target_entity": "ike",
                    "session_name": "ike",
                    "outcome": "delivered",
                }
            ],
        ]
        mock_db.is_acknowledged.return_value = False

        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_IDLE_SNAP),
            _patch_send_message(True),
        ):
            result = await safe_deliver(
                "ike",
                "Hello",
                config,
                db=mock_db,
                issue_number=42,
                target_entity="ike",
                flow_name="test_flow",
                enforce_issue_queue=True,
                queue_scope_issue_numbers={42, 43},
            )

        assert result == "delivered"
        mock_db.record_delivery.assert_called_once_with(
            issue_number=42,
            target_entity="ike",
            session_name="ike",
            outcome="delivered",
            flow_name="test_flow",
        )

    async def test_comment_delivery_records_distinct_outcome(self):
        """Comment notifications should not be persisted as issue deliveries."""
        config = _default_config()
        mock_db = AsyncMock()

        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_IDLE_SNAP),
            _patch_send_message(True),
        ):
            result = await safe_deliver(
                "ike",
                "Hello",
                config,
                db=mock_db,
                issue_number=42,
                target_entity="ike",
                flow_name="test_flow",
                delivery_kind="comment",
            )

        assert result == "delivered"
        mock_db.record_delivery.assert_called_once_with(
            issue_number=42,
            target_entity="ike",
            session_name="ike",
            outcome="comment_delivered",
            flow_name="test_flow",
        )

    async def test_comment_delivery_bypasses_agent_working_for_current_issue(self):
        """Comments on the active processing issue should deliver immediately."""
        config = _default_config()
        mock_db = AsyncMock()

        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_PROCESSING_ISSUE_42_SNAP),
            _patch_send_message(True),
        ):
            result = await safe_deliver(
                "ike",
                "Comment",
                config,
                db=mock_db,
                issue_number=42,
                target_entity="ike",
                flow_name="test_flow",
                delivery_kind="comment",
            )

        assert result == "delivered"
        mock_db.record_delivery.assert_called_once_with(
            issue_number=42,
            target_entity="ike",
            session_name="ike",
            outcome="comment_delivered",
            flow_name="test_flow",
        )

    async def test_comment_delivery_to_different_issue_is_queued_while_busy(self):
        """Comments on a different processing issue should defer durably."""
        config = _default_config()
        mock_db = AsyncMock()

        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_PROCESSING_ISSUE_99_SNAP),
        ):
            result = await safe_deliver(
                "ike",
                "Comment",
                config,
                db=mock_db,
                issue_number=42,
                target_entity="ike",
                flow_name="test_flow",
                delivery_kind="comment",
            )

        assert result == "agent_working"
        mock_db.enqueue_message.assert_called_once_with(
            session_name="ike",
            message="Comment",
            issue_number=42,
            target_entity="ike",
            delivery_kind="comment",
            flow_name="test_flow",
        )
        mock_db.record_delivery.assert_called_once_with(
            issue_number=42,
            target_entity="ike",
            session_name="ike",
            outcome="comment_agent_working",
            flow_name="test_flow",
        )

    async def test_comment_delivery_bypasses_plan_waiting_for_current_issue(self):
        """Same-issue comments should reach plan_waiting sessions."""
        config = _default_config()
        mock_db = AsyncMock()

        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_PLAN_WAITING_ISSUE_42_SNAP),
            _patch_send_message(True),
        ):
            result = await safe_deliver(
                "ike",
                "Comment",
                config,
                db=mock_db,
                issue_number=42,
                target_entity="ike",
                flow_name="test_flow",
                delivery_kind="comment",
            )

        assert result == "delivered"

    async def test_comment_delivery_bypasses_permission_waiting_for_current_issue(self):
        """Same-issue comments should reach permission-waiting sessions."""
        config = _default_config()
        mock_db = AsyncMock()

        with (
            _patch_list_sessions(["ike"]),
            _patch_query_format_vars({"pane_in_mode": "0", "client_activity": "0"}),
            _patch_get_agent_state(_PERMISSION_WAITING_ISSUE_42_SNAP),
            _patch_send_message(True),
        ):
            result = await safe_deliver(
                "ike",
                "Comment",
                config,
                db=mock_db,
                issue_number=42,
                target_entity="ike",
                flow_name="test_flow",
                delivery_kind="comment",
            )

        assert result == "delivered"

    async def test_comment_delivery_still_respects_copy_mode(self):
        """Comments should attempt recovery before falling back to queue/failure."""
        config = _default_config()
        mock_db = AsyncMock()
        copy_profile = SessionProfile(
            session_name="ike",
            intelligence=SessionIntelligence.COPY_MODE,
            runtime="codex",
            agent_state=AgentState.IDLE,
        )
        adapter = AsyncMock()
        adapter.exit_copy_mode.return_value = True

        with (
            patch(
                "agent_backbone.services.routing._delivery.get_session_intelligence",
                new_callable=AsyncMock,
                side_effect=[copy_profile, copy_profile],
            ),
            patch(
                "agent_backbone.services.routing._delivery.get_terminal_adapter",
                return_value=adapter,
            ),
        ):
            result = await safe_deliver(
                "ike",
                "Comment",
                config,
                db=mock_db,
                issue_number=42,
                target_entity="ike",
                flow_name="test_flow",
                delivery_kind="comment",
            )

        assert result == "delivery_failed"

    async def test_grace_period_defers(self):
        """IDLE_GRACE intelligence returns 'grace_period' outcome."""
        config = _default_config()
        with patch(
            "agent_backbone.services.routing._delivery.get_session_intelligence",
            new_callable=AsyncMock,
            return_value=SessionProfile(
                session_name="ike",
                intelligence=SessionIntelligence.IDLE_GRACE,
                agent_state=AgentState.IDLE,
            ),
        ):
            result = await safe_deliver("ike", "Hello", config)
        assert result == "grace_period"

    async def test_jarvis_http_delivery(self):
        """Jarvis HTTP target delivers via inject_message, returns 'delivered'."""
        config = BackboneConfig(
            webhook_secret="s",
            jarvis=JarvisConfig(inject_url="http://localhost:3000/api/assistant/inject"),
        )
        with patch(
            "agent_backbone.jarvis.inject_message",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_inject:
            result = await safe_deliver("jarvis", "Hello Jarvis", config)

        assert result == "delivered"
        mock_inject.assert_called_once_with(
            "http://localhost:3000/api/assistant/inject", "Hello Jarvis", sessions_url=""
        )

    async def test_jarvis_http_failure_enqueues(self):
        """Jarvis HTTP failure returns 'delivery_failed' and enqueues."""
        config = BackboneConfig(
            webhook_secret="s",
            jarvis=JarvisConfig(inject_url="http://localhost:3000/api/assistant/inject"),
        )
        mock_db = AsyncMock()
        with patch(
            "agent_backbone.jarvis.inject_message",
            new_callable=AsyncMock,
            return_value=False,
        ):
            result = await safe_deliver(
                "jarvis",
                "Hello",
                config,
                db=mock_db,
                issue_number=42,
                target_entity="jarvis",
                flow_name="test_flow",
            )

        assert result == "delivery_failed"
        mock_db.enqueue_message.assert_called_once_with(
            session_name="jarvis",
            message="Hello",
            issue_number=42,
            target_entity="jarvis",
            delivery_kind="issue",
            flow_name="test_flow",
        )


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
                "agent_backbone.services.terminal.list_sessions_rich",
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
        with patch(
            "agent_backbone.services.terminal.list_sessions_rich",
            new_callable=AsyncMock,
            return_value=[],
        ):
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
                "agent_backbone.services.terminal.list_sessions_rich",
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
                "agent_backbone.services.terminal.list_sessions_rich",
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
