"""The integrations contract: registry fan-out, static notifications, Telegram surfaces."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, patch

from agent_backbone.config import TelegramConfig
from agent_backbone.services.integrations import (
    Integration,
    Integrations,
    build_integrations,
    notify_humans,
)
from agent_backbone.services.integrations.telegram import TelegramService
from agent_backbone.services.integrations.telegram._topic_discovery import TopicDiscovery
from agent_backbone.services.integrations.telegram.interface import agent_topic, notify_static

_TG = "agent_backbone.services.integrations.telegram.interface"


class _Fake(Integration):
    name = "fake"

    def __init__(self, *, enabled=True, surfaces=()):
        super().__init__(lambda: None)
        self._enabled = enabled
        self._surfaces = set(surfaces)
        self.replies: list[tuple[str, str]] = []
        self.synced = 0

    @property
    def enabled(self):
        return self._enabled

    async def reply_to_agent(self, agent, text):
        if agent not in self._surfaces:
            return False
        self.replies.append((agent, text))
        return True

    async def sync_agents(self):
        self.synced += 1


class TestRegistry:
    def test_build_ships_telegram_inert_without_token(self, config):
        integrations = build_integrations(lambda: config)
        assert [i.name for i in integrations] == ["telegram"]
        assert integrations.enabled == []
        assert integrations.health() == {"telegram": "disabled"}
        assert isinstance(integrations.get("telegram"), TelegramService)

    async def test_reply_fans_out_only_to_enabled_with_a_surface(self):
        with_surface = _Fake(surfaces={"ike"})
        without = _Fake()
        disabled = _Fake(enabled=False, surfaces={"ike"})
        integrations = Integrations([with_surface, without, disabled])
        # names collide on purpose: results are keyed by name, last write wins
        without.name = "other"
        disabled.name = "off"
        assert await integrations.reply_to_agent("ike", "done") == {"fake": True, "other": False}
        assert with_surface.replies == [("ike", "done")]

    async def test_sync_runs_every_enabled_integration_and_survives_errors(self):
        good = _Fake()
        bad = _Fake()
        bad.name = "bad"
        bad.sync_agents = AsyncMock(side_effect=RuntimeError("boom"))
        await Integrations([bad, good]).sync_agents()
        assert good.synced == 1

    def test_health_reports_running_state(self):
        up = _Fake()
        up._running = True
        down = _Fake()
        down.name = "down"
        off = _Fake(enabled=False)
        off.name = "off"
        assert Integrations([up, down, off]).health() == {
            "fake": "up",
            "down": "down",
            "off": "disabled",
        }


class TestNotifyHumans:
    async def test_fans_out_and_reports_any_success(self, config):
        with patch(f"{_TG}.notify_static", new_callable=AsyncMock, return_value=True) as tg:
            assert await notify_humans(config, "hello", agent="ike") is True
        tg.assert_awaited_once_with(config, "hello", agent="ike")

    async def test_nothing_configured_is_false_not_an_error(self, config):
        assert await notify_humans(config, "hello") is False

    async def test_integration_errors_never_propagate(self, config):
        with patch(f"{_TG}.notify_static", new_callable=AsyncMock, side_effect=RuntimeError):
            assert await notify_humans(config, "hello") is False


class TestTelegramSurfaces:
    def test_agent_topic_prefers_explicit_config(self, config):
        cfg = replace(config, telegram=TelegramConfig(topic_routes={7: "ike"}))
        discovery = TopicDiscovery(topic_routes={9: "ike", 3: "agents"})
        assert agent_topic(cfg, discovery, "ike") == 7
        assert agent_topic(config, discovery, "ike") == 9
        assert agent_topic(config, discovery, "feynman") is None

    async def test_notify_static_goes_into_the_agents_topic(self, config):
        cfg = replace(
            config,
            telegram_token="t",
            telegram=TelegramConfig(
                group_chat_id=-100, topic_routes={7: "ike"}, notification_chat_id=555
            ),
        )
        with patch(f"{_TG}._send", new_callable=AsyncMock, return_value=True) as send:
            assert await notify_static(cfg, "plan waiting", agent="ike")
            assert await notify_static(cfg, "general alert")
            assert await notify_static(cfg, "no topic yet", agent="feynman")
        assert [c.args[1:] + (c.kwargs.get("thread_id"),) for c in send.await_args_list] == [
            (-100, "plan waiting", 7),
            (555, "general alert", None),
            (555, "no topic yet", None),
        ]

    async def test_notify_static_without_any_destination_is_false(self, config):
        cfg = replace(config, telegram_token="t")
        with patch(f"{_TG}._send", new_callable=AsyncMock) as send:
            assert await notify_static(cfg, "x", agent="ike") is False
        send.assert_not_called()

    async def test_reply_to_agent_needs_a_topic(self, config):
        cfg = replace(
            config,
            telegram_token="t",
            telegram=TelegramConfig(group_chat_id=-100, topic_routes={7: "ike"}),
        )
        bot = TelegramService(cfg)
        with patch(f"{_TG}._send", new_callable=AsyncMock, return_value=True) as send:
            assert await bot.reply_to_agent("ike", "done") is True
            assert await bot.reply_to_agent("feynman", "done") is False
        send.assert_awaited_once_with("t", -100, "done", thread_id=7)
