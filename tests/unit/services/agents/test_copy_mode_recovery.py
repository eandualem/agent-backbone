"""Tests for copy-mode auto-recovery in the agent monitor."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.config import (
    AgentStateConfig,
    BackboneConfig,
    CapacityRoutingConfig,
    DeliveryConfig,
    EntityConfig,
    EscalationConfig,
    GitHubConfig,
    TelegramConfig,
)
from agent_backbone.services.agents._copy_mode import (
    _copy_mode_incidents,
    _last_non_copy_state,
    handle_copy_mode_recovery,
)
from agent_backbone.services.agents.models import AgentState, StateSnapshot
from agent_backbone.services.registry import EntityEntry, EntityRegistry, RepoInfo

_COPY = "agent_backbone.services.agents._copy_mode"


def _make_config(*, notification_chat_id: int | None = None) -> BackboneConfig:
    """Minimal config with one named entity and one coding repo."""
    registry = EntityRegistry(
        entities={
            "feynman": EntityEntry(
                session="feynman",
                home="~/ws/feynman",
                groups=[],
                figure="",
                role="",
            ),
        },
        repos=[RepoInfo(org="WF", name="agent-backbone", path="/tmp/agent-backbone")],
    )
    return BackboneConfig(
        webhook_secret="test-secret",
        github=GitHubConfig(owner="eandualem", repo="orchestration"),
        entities=EntityConfig(skip=frozenset({"elias"})),
        registry=registry,
        agent_state=AgentStateConfig(
            state_dir="/tmp/test-state",
            stale_threshold_seconds=300,
        ),
        escalation=EscalationConfig(),
        delivery=DeliveryConfig(),
        capacity_routing=CapacityRoutingConfig(),
        telegram=TelegramConfig(notification_chat_id=notification_chat_id),
    )


@pytest.fixture(autouse=True)
def clear_copy_mode_state():
    """Reset in-memory copy-mode trackers between tests."""
    _copy_mode_incidents.clear()
    _last_non_copy_state.clear()
    yield
    _copy_mode_incidents.clear()
    _last_non_copy_state.clear()


class TestCopyModeRecovery:
    @pytest.mark.asyncio
    async def test_auto_exit_sent_after_threshold(self):
        config = _make_config()
        idle_snapshot = StateSnapshot(state=AgentState.IDLE, source="pull")
        clock = {"now": 100.0}

        with (
            patch(f"{_COPY}.time.monotonic", side_effect=lambda: clock["now"]),
            patch(f"{_COPY}.get_agent_state", new_callable=AsyncMock, return_value=idle_snapshot),
            patch(
                f"{_COPY}.query_format_vars",
                new_callable=AsyncMock,
                return_value={"pane_in_mode": "1"},
            ),
            patch(f"{_COPY}.send_keys", new_callable=AsyncMock, return_value=True) as mock_send,
        ):
            await handle_copy_mode_recovery(config, {"feynman"})
            mock_send.assert_not_awaited()

            clock["now"] = 131.0
            await handle_copy_mode_recovery(config, {"feynman"})

        mock_send.assert_awaited_once_with("feynman", "q")

    @pytest.mark.asyncio
    async def test_persistent_copy_mode_sends_telegram_alert(self):
        config = _make_config(notification_chat_id=12345)
        idle_snapshot = StateSnapshot(state=AgentState.IDLE, source="pull")
        clock = {"now": 10.0}

        with (
            patch.dict(os.environ, {"TELEGRAM_TOKEN": "test-token"}),
            patch(f"{_COPY}.time.monotonic", side_effect=lambda: clock["now"]),
            patch(f"{_COPY}.get_agent_state", new_callable=AsyncMock, return_value=idle_snapshot),
            patch(
                f"{_COPY}.query_format_vars",
                new_callable=AsyncMock,
                return_value={"pane_in_mode": "1"},
            ),
            patch(f"{_COPY}.send_keys", new_callable=AsyncMock, return_value=True),
            patch(
                f"{_COPY}.TelegramService.send_notification",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_notify,
        ):
            await handle_copy_mode_recovery(config, {"feynman"})
            clock["now"] = 41.0
            await handle_copy_mode_recovery(config, {"feynman"})
            clock["now"] = 162.0
            await handle_copy_mode_recovery(config, {"feynman"})

        mock_notify.assert_awaited_once()
        args = mock_notify.await_args.args
        assert args[0] == "test-token"
        assert args[1] == 12345
        assert "feynman" in args[2]
        assert "preceded by: idle" in args[2].lower()

    @pytest.mark.asyncio
    async def test_recovery_clears_incident_state(self):
        config = _make_config()
        idle_snapshot = StateSnapshot(state=AgentState.IDLE, source="pull")
        clock = {"now": 0.0}
        tmux_values = [{"pane_in_mode": "1"}, {"pane_in_mode": "0"}]

        with (
            patch(f"{_COPY}.time.monotonic", side_effect=lambda: clock["now"]),
            patch(f"{_COPY}.get_agent_state", new_callable=AsyncMock, return_value=idle_snapshot),
            patch(
                f"{_COPY}.query_format_vars",
                new_callable=AsyncMock,
                side_effect=lambda *args, **kwargs: tmux_values.pop(0),
            ),
            patch(f"{_COPY}.send_keys", new_callable=AsyncMock, return_value=True),
        ):
            await handle_copy_mode_recovery(config, {"feynman"})
            assert "feynman" in _copy_mode_incidents

            clock["now"] = 45.0
            await handle_copy_mode_recovery(config, {"feynman"})

        assert "feynman" not in _copy_mode_incidents
        assert _last_non_copy_state["feynman"] == AgentState.IDLE.value

    @pytest.mark.asyncio
    async def test_working_state_does_not_trigger_recovery(self):
        config = _make_config()
        busy_snapshot = StateSnapshot(state=AgentState.BUSY, source="pull")

        with (
            patch(f"{_COPY}.get_agent_state", new_callable=AsyncMock, return_value=busy_snapshot),
            patch(
                f"{_COPY}.query_format_vars",
                new_callable=AsyncMock,
                return_value={"pane_in_mode": "1"},
            ),
            patch(f"{_COPY}.send_keys", new_callable=AsyncMock, return_value=True) as mock_send,
        ):
            await handle_copy_mode_recovery(config, {"feynman"})

        mock_send.assert_not_awaited()
        assert _copy_mode_incidents == {}
