"""Tests for agent_backbone/services/telegram."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import respx

from agent_backbone.config import AgentStateConfig, BackboneConfig, TelegramConfig
from agent_backbone.services.registry import EntityEntry, EntityRegistry
from agent_backbone.services.telegram import TelegramService, _delivery_reply
from agent_backbone.services.telegram._topic_discovery import TopicDiscovery
from agent_backbone.services.terminal import RUNTIME_ENV_KEY


def _make_topic_update(thread_id: int | None, text: str, chat_id: int = 100) -> MagicMock:
    """Create a mock Update with topic message attributes."""
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.message = MagicMock()
    update.message.text = text
    update.message.message_thread_id = thread_id
    update.message.reply_text = AsyncMock()
    # Prevent process_message_for_discovery from traversing mock auto-attributes
    update.message.reply_to_message = None
    return update


class TestAuthorization:
    def test_unrestricted_when_empty(self):
        """Empty allowed_chat_ids means unrestricted access."""
        config = BackboneConfig(telegram=TelegramConfig(allowed_chat_ids=[]))
        with patch(
            "agent_backbone.services.telegram.interface.load_discovery",
            return_value=TopicDiscovery(),
        ):
            bot = TelegramService(config)
        assert bot._is_authorized(12345) is True
        assert bot._is_authorized(99999) is True

    def test_restricted_when_populated(self):
        """Only listed chat IDs are authorized."""
        config = BackboneConfig(telegram=TelegramConfig(allowed_chat_ids=[111, 222]))
        with patch(
            "agent_backbone.services.telegram.interface.load_discovery",
            return_value=TopicDiscovery(),
        ):
            bot = TelegramService(config)
        assert bot._is_authorized(111) is True
        assert bot._is_authorized(222) is True
        assert bot._is_authorized(333) is False


class TestTopicRouting:
    """Tests for handle_topic_message — topic-based message routing."""

    @pytest.fixture
    def bot_with_routes(self, tmp_path):
        config = BackboneConfig(
            telegram=TelegramConfig(
                allowed_chat_ids=[100],
                topic_routes={10: "leo", 20: "agent-backbone", 30: "coding-agents"},
                topic_discovery_file=str(tmp_path / "topics.json"),
            )
        )
        with patch(
            "agent_backbone.services.telegram.interface.load_discovery",
            return_value=TopicDiscovery(),
        ):
            return TelegramService(config)

    @pytest.mark.asyncio
    async def test_direct_topic_to_mapped_session(self, bot_with_routes):
        """Message in a directly-mapped topic goes to that session."""
        update = _make_topic_update(thread_id=10, text="Hello Leo")
        mock_kw = dict(new_callable=AsyncMock, return_value="delivered")
        with patch(
            "agent_backbone.services.telegram._routing.deliver_message", **mock_kw
        ) as mock_send:
            await bot_with_routes.handle_topic_message(update, MagicMock())
            args = mock_send.call_args[0]
            assert args[0] == "leo"
            assert "[via:telegram from:" in args[1]
            assert "Hello Leo" in args[1]
        update.message.reply_text.assert_awaited_once()
        assert "leo" in update.message.reply_text.call_args[0][0]

    @pytest.mark.asyncio
    async def test_coding_agents_colon_format(self, bot_with_routes):
        """Coding-agents topic parses 'agent: message' format."""
        update = _make_topic_update(thread_id=30, text="platform-api: fix the auth bug")
        mock_kw = dict(new_callable=AsyncMock, return_value="delivered")
        with patch(
            "agent_backbone.services.telegram._routing.deliver_message", **mock_kw
        ) as mock_send:
            await bot_with_routes.handle_topic_message(update, MagicMock())
            args = mock_send.call_args[0]
            assert args[0] == "platform-api"
            assert "[via:telegram from:" in args[1]
            assert "fix the auth bug" in args[1]

    @pytest.mark.asyncio
    async def test_coding_agents_space_format(self, bot_with_routes):
        """Coding-agents topic parses 'agent message' format (no colon)."""
        update = _make_topic_update(thread_id=30, text="mcp-hub check the logs")
        mock_kw = dict(new_callable=AsyncMock, return_value="delivered")
        with patch(
            "agent_backbone.services.telegram._routing.deliver_message", **mock_kw
        ) as mock_send:
            await bot_with_routes.handle_topic_message(update, MagicMock())
            args = mock_send.call_args[0]
            assert args[0] == "mcp-hub"
            assert "[via:telegram from:" in args[1]
            assert "check the logs" in args[1]

    @pytest.mark.asyncio
    async def test_coding_agents_no_message_body(self, bot_with_routes):
        """Coding-agents topic with agent name only shows usage hint."""
        update = _make_topic_update(thread_id=30, text="platform-api:")
        with patch(
            "agent_backbone.services.telegram._routing.deliver_message", new_callable=AsyncMock
        ) as mock_send:
            await bot_with_routes.handle_topic_message(update, MagicMock())
            mock_send.assert_not_awaited()
        assert "Usage" in update.message.reply_text.call_args[0][0]

    @pytest.mark.asyncio
    async def test_unmapped_topic_ignored(self, bot_with_routes):
        """Message in an unmapped topic is silently ignored."""
        update = _make_topic_update(thread_id=999, text="hello")
        with patch(
            "agent_backbone.services.telegram._routing.deliver_message", new_callable=AsyncMock
        ) as mock_send:
            await bot_with_routes.handle_topic_message(update, MagicMock())
            mock_send.assert_not_awaited()
        update.message.reply_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_topic_message_ignored(self, bot_with_routes):
        """Message without thread_id is ignored."""
        update = _make_topic_update(thread_id=None, text="hello")
        with patch(
            "agent_backbone.services.telegram._routing.deliver_message", new_callable=AsyncMock
        ) as mock_send:
            await bot_with_routes.handle_topic_message(update, MagicMock())
            mock_send.assert_not_awaited()
        update.message.reply_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unauthorized_chat_ignored(self, bot_with_routes):
        """Message from unauthorized chat is ignored."""
        update = _make_topic_update(thread_id=10, text="hello", chat_id=999)
        with patch(
            "agent_backbone.services.telegram._routing.deliver_message", new_callable=AsyncMock
        ) as mock_send:
            await bot_with_routes.handle_topic_message(update, MagicMock())
            mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_failure_reports_offline(self, bot_with_routes):
        """Offline deliver_message result reports agent offline."""
        update = _make_topic_update(thread_id=10, text="hello")
        with patch(
            "agent_backbone.services.telegram._routing.deliver_message",
            new_callable=AsyncMock,
            return_value="offline",
        ):
            await bot_with_routes.handle_topic_message(update, MagicMock())
        assert "offline" in update.message.reply_text.call_args[0][0]

    @pytest.mark.asyncio
    async def test_empty_topic_routes_ignores_all(self, tmp_path):
        """Bot with no topic_routes ignores all topic messages."""
        config = BackboneConfig(
            telegram=TelegramConfig(
                allowed_chat_ids=[],
                topic_routes={},
                topic_discovery_file=str(tmp_path / "topics.json"),
            )
        )
        with patch(
            "agent_backbone.services.telegram.interface.load_discovery",
            return_value=TopicDiscovery(),
        ):
            bot = TelegramService(config)
        update = _make_topic_update(thread_id=10, text="hello")
        with patch(
            "agent_backbone.services.telegram._routing.deliver_message", new_callable=AsyncMock
        ) as mock_send:
            await bot.handle_topic_message(update, MagicMock())
            mock_send.assert_not_awaited()
        update.message.reply_text.assert_not_awaited()


class TestIdentifyCommand:
    """Tests for cmd_identify — topic thread ID discovery."""

    @pytest.fixture
    def bot_with_routes(self, tmp_path):
        config = BackboneConfig(
            telegram=TelegramConfig(
                allowed_chat_ids=[],
                topic_routes={10: "leo"},
                topic_discovery_file=str(tmp_path / "topics.json"),
            )
        )
        with patch(
            "agent_backbone.services.telegram.interface.load_discovery",
            return_value=TopicDiscovery(),
        ):
            return TelegramService(config)

    @pytest.mark.asyncio
    async def test_unmapped_topic(self, bot_with_routes):
        """Reports thread_id and 'Not yet mapped' for unmapped topics."""
        update = _make_topic_update(thread_id=99, text="/identify")
        await bot_with_routes.cmd_identify(update, MagicMock())
        reply = update.message.reply_text.call_args[0][0]
        assert "99" in reply
        assert "Not yet mapped" in reply

    @pytest.mark.asyncio
    async def test_mapped_topic(self, bot_with_routes):
        """Reports thread_id and mapping for configured topics."""
        update = _make_topic_update(thread_id=10, text="/identify")
        await bot_with_routes.cmd_identify(update, MagicMock())
        reply = update.message.reply_text.call_args[0][0]
        assert "10" in reply
        assert "leo" in reply

    @pytest.mark.asyncio
    async def test_outside_topic(self, bot_with_routes):
        """Reports 'Not in a topic' when not in a forum topic."""
        update = _make_topic_update(thread_id=None, text="/identify")
        await bot_with_routes.cmd_identify(update, MagicMock())
        reply = update.message.reply_text.call_args[0][0]
        assert "Not in a topic" in reply

    @pytest.mark.asyncio
    async def test_shows_config_source(self, bot_with_routes):
        """Mapped topic from config shows '(config)' source."""
        update = _make_topic_update(thread_id=10, text="/identify")
        await bot_with_routes.cmd_identify(update, MagicMock())
        reply = update.message.reply_text.call_args[0][0]
        assert "config" in reply
        assert "leo" in reply

    @pytest.mark.asyncio
    async def test_shows_auto_discovered_source(self, tmp_path):
        """Mapped topic from discovery shows '(auto-discovered)' source."""
        config = BackboneConfig(
            telegram=TelegramConfig(
                allowed_chat_ids=[],
                topic_routes={},
                topic_discovery_file=str(tmp_path / "topics.json"),
            )
        )
        discovery = TopicDiscovery(topic_routes={10: "leo"})
        with patch(
            "agent_backbone.services.telegram.interface.load_discovery", return_value=discovery
        ):
            bot = TelegramService(config)
        update = _make_topic_update(thread_id=10, text="/identify")
        await bot.cmd_identify(update, MagicMock())
        reply = update.message.reply_text.call_args[0][0]
        assert "auto-discovered" in reply
        assert "leo" in reply


class TestDiscoveryIntegration:
    """Tests for discovery-aware routing in the bot."""

    @pytest.mark.asyncio
    async def test_discovery_route_used_when_config_empty(self, tmp_path):
        """Bot with empty config routes uses discovered routes."""
        config = BackboneConfig(
            telegram=TelegramConfig(
                allowed_chat_ids=[],
                topic_routes={},
                topic_discovery_file=str(tmp_path / "topics.json"),
            )
        )
        discovery = TopicDiscovery(topic_routes={10: "leo"})
        with patch(
            "agent_backbone.services.telegram.interface.load_discovery", return_value=discovery
        ):
            bot = TelegramService(config)

        update = _make_topic_update(thread_id=10, text="hello from discovery")
        mock_kw = dict(new_callable=AsyncMock, return_value="delivered")
        with patch(
            "agent_backbone.services.telegram._routing.deliver_message", **mock_kw
        ) as mock_send:
            await bot.handle_topic_message(update, MagicMock())
            args = mock_send.call_args[0]
            assert args[0] == "leo"
            assert "hello from discovery" in args[1]

    @pytest.mark.asyncio
    async def test_config_override_beats_discovery(self, tmp_path):
        """Config route takes precedence over discovery for same thread_id."""
        config = BackboneConfig(
            telegram=TelegramConfig(
                allowed_chat_ids=[],
                topic_routes={10: "ike"},
                topic_discovery_file=str(tmp_path / "topics.json"),
            )
        )
        discovery = TopicDiscovery(topic_routes={10: "leo"})
        with patch(
            "agent_backbone.services.telegram.interface.load_discovery", return_value=discovery
        ):
            bot = TelegramService(config)

        update = _make_topic_update(thread_id=10, text="who gets this?")
        mock_kw = dict(new_callable=AsyncMock, return_value="delivered")
        with patch(
            "agent_backbone.services.telegram._routing.deliver_message", **mock_kw
        ) as mock_send:
            await bot.handle_topic_message(update, MagicMock())
            args = mock_send.call_args[0]
            assert args[0] == "ike"
            assert "who gets this?" in args[1]


class TestViewPlanCommand:
    @pytest.fixture
    def bot(self, tmp_path):
        config = BackboneConfig(
            telegram=TelegramConfig(
                allowed_chat_ids=[],
                topic_discovery_file=str(tmp_path / "topics.json"),
            )
        )
        with patch(
            "agent_backbone.services.telegram.interface.load_discovery",
            return_value=TopicDiscovery(),
        ):
            return TelegramService(config)

    @pytest.mark.asyncio
    async def test_viewplan_no_args(self, bot):
        update = _make_topic_update(thread_id=None, text="/viewplan")
        ctx = MagicMock()
        ctx.args = []
        await bot.cmd_viewplan(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "Usage" in reply

    @pytest.mark.asyncio
    async def test_viewplan_not_waiting(self, bot, tmp_path):
        # Write idle state file
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "ike.json").write_text('{"state": "idle", "ts": 1000000}')
        bot._config = BackboneConfig(
            telegram=TelegramConfig(
                allowed_chat_ids=[],
                topic_discovery_file=str(tmp_path / "topics.json"),
            ),
            agent_state=AgentStateConfig(
                state_dir=str(state_dir),
            ),
        )
        update = _make_topic_update(thread_id=None, text="/viewplan ike")
        ctx = MagicMock()
        ctx.args = ["ike"]
        await bot.cmd_viewplan(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "not waiting" in reply

    @pytest.mark.asyncio
    async def test_viewplan_shows_plan(self, bot, tmp_path):
        # Write plan file
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("# My Plan\nStep 1: do stuff\nStep 2: profit")
        # Write plan_waiting state
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        import json
        import time

        (state_dir / "feynman.json").write_text(
            json.dumps(
                {
                    "state": "plan_waiting",
                    "ts": time.time(),
                    "plan_file": str(plan_file),
                    "plan_title": "My Plan",
                }
            )
        )
        bot._config = BackboneConfig(
            telegram=TelegramConfig(
                allowed_chat_ids=[],
                topic_discovery_file=str(tmp_path / "topics.json"),
            ),
            agent_state=AgentStateConfig(state_dir=str(state_dir)),
        )
        update = _make_topic_update(thread_id=None, text="/viewplan feynman")
        ctx = MagicMock()
        ctx.args = ["feynman"]
        await bot.cmd_viewplan(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "My Plan" in reply
        assert "Step 1" in reply


class TestApproveCommand:
    @pytest.fixture
    def bot(self, tmp_path):
        config = BackboneConfig(
            telegram=TelegramConfig(
                allowed_chat_ids=[],
                topic_discovery_file=str(tmp_path / "topics.json"),
            )
        )
        with patch(
            "agent_backbone.services.telegram.interface.load_discovery",
            return_value=TopicDiscovery(),
        ):
            return TelegramService(config)

    @pytest.mark.asyncio
    async def test_approve_not_waiting(self, bot, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "ike.json").write_text('{"state": "idle", "ts": 1000000}')
        bot._config = BackboneConfig(
            telegram=TelegramConfig(
                allowed_chat_ids=[],
                topic_discovery_file=str(tmp_path / "topics.json"),
            ),
            agent_state=AgentStateConfig(state_dir=str(state_dir)),
        )
        update = _make_topic_update(thread_id=None, text="/approve ike")
        ctx = MagicMock()
        ctx.args = ["ike"]
        await bot.cmd_approve(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "not waiting" in reply

    @pytest.mark.asyncio
    async def test_approve_session_offline(self, bot, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        import json
        import time

        (state_dir / "ike.json").write_text(
            json.dumps(
                {
                    "state": "plan_waiting",
                    "ts": time.time(),
                    "plan_file": "/tmp/plan.md",
                    "plan_title": "Test",
                }
            )
        )
        bot._config = BackboneConfig(
            telegram=TelegramConfig(
                allowed_chat_ids=[],
                topic_discovery_file=str(tmp_path / "topics.json"),
            ),
            agent_state=AgentStateConfig(state_dir=str(state_dir)),
        )
        update = _make_topic_update(thread_id=None, text="/approve ike")
        ctx = MagicMock()
        ctx.args = ["ike"]
        with patch(
            "agent_backbone.services.telegram._commands.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            await bot.cmd_approve(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "offline" in reply

    @pytest.mark.asyncio
    async def test_approve_sends_keys(self, bot, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        import json
        import time

        (state_dir / "ike.json").write_text(
            json.dumps(
                {
                    "state": "plan_waiting",
                    "ts": time.time(),
                    "plan_file": "/tmp/plan.md",
                    "plan_title": "Test",
                }
            )
        )
        bot._config = BackboneConfig(
            telegram=TelegramConfig(
                allowed_chat_ids=[],
                topic_discovery_file=str(tmp_path / "topics.json"),
            ),
            agent_state=AgentStateConfig(state_dir=str(state_dir)),
        )
        update = _make_topic_update(thread_id=None, text="/approve ike")
        ctx = MagicMock()
        ctx.args = ["ike"]
        with (
            patch(
                "agent_backbone.services.telegram._commands.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.telegram._commands.send_keys",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_keys,
        ):
            await bot.cmd_approve(update, ctx)
        # Should send Escape then [Z
        assert mock_keys.call_count == 2
        calls = [c[0] for c in mock_keys.call_args_list]
        assert calls[0] == ("ike", "Escape")
        assert calls[1] == ("ike", "[Z")
        reply = update.message.reply_text.call_args[0][0]
        assert "approved" in reply.lower()


class TestDeliveryReply:
    """Tests for _delivery_reply feedback and deliver_message integration."""

    @pytest.fixture
    def bot(self, tmp_path):
        config = BackboneConfig(
            telegram=TelegramConfig(
                allowed_chat_ids=[],
                topic_routes={10: "leo"},
                topic_discovery_file=str(tmp_path / "topics.json"),
            )
        )
        with patch(
            "agent_backbone.services.telegram.interface.load_discovery",
            return_value=TopicDiscovery(),
        ):
            return TelegramService(config)

    @pytest.mark.asyncio
    async def test_cmd_tell_uses_deliver_message(self, bot):
        """cmd_tell calls deliver_message and replies 'Sent' on delivered."""
        update = _make_topic_update(thread_id=None, text="/tell feynman hello")
        ctx = MagicMock()
        ctx.args = ["feynman", "hello"]
        _tgt = "agent_backbone.services.telegram._commands.deliver_message"
        mock_kw = dict(new_callable=AsyncMock, return_value="delivered")
        with patch(_tgt, **mock_kw) as mock_sd:
            await bot.cmd_tell(update, ctx)
            args = mock_sd.call_args[0]
            assert args[0] == "feynman"
            assert "hello" in args[1]
            assert args[2] is bot._config
        reply = update.message.reply_text.call_args[0][0]
        assert "Sent" in reply
        assert "feynman" in reply

    @pytest.mark.asyncio
    async def test_cmd_tell_agent_busy(self, bot):
        """cmd_tell reports 'busy' when deliver_message returns agent_working."""
        update = _make_topic_update(thread_id=None, text="/tell ike check status")
        ctx = MagicMock()
        ctx.args = ["ike", "check", "status"]
        mock_kw = dict(new_callable=AsyncMock, return_value="agent_working")
        with patch("agent_backbone.services.telegram._commands.deliver_message", **mock_kw):
            await bot.cmd_tell(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "busy" in reply
        assert "ike" in reply

    @pytest.mark.asyncio
    async def test_topic_message_agent_plan_waiting(self, bot):
        """Topic message reports plan approval when deliver_message returns plan_waiting."""
        update = _make_topic_update(thread_id=10, text="check this")
        mock_kw = dict(new_callable=AsyncMock, return_value="plan_waiting")
        with patch("agent_backbone.services.telegram._routing.deliver_message", **mock_kw):
            await bot.handle_topic_message(update, MagicMock())
        reply = update.message.reply_text.call_args[0][0]
        assert "plan approval" in reply
        assert "leo" in reply


class TestDeliveryReplyFallbacks:
    """Tests for _delivery_reply fallback path — statuses that return 'Not delivered'."""

    def test_copy_mode_fallback(self):
        reply = _delivery_reply("leo", "copy_mode")
        assert "Not delivered" in reply
        assert "copy_mode" in reply

    def test_user_interacting_fallback(self):
        reply = _delivery_reply("leo", "user_interacting")
        assert "Not delivered" in reply
        assert "user_interacting" in reply

    def test_grace_period_fallback(self):
        reply = _delivery_reply("leo", "grace_period")
        assert "Not delivered" in reply
        assert "grace_period" in reply

    def test_delivery_failed_fallback(self):
        reply = _delivery_reply("leo", "delivery_failed")
        assert "Not delivered" in reply
        assert "delivery_failed" in reply


class TestSendNotification:
    @pytest.mark.asyncio
    @respx.mock
    async def test_send_notification_success(self):
        url = "https://api.telegram.org/botTEST_TOKEN/sendMessage"
        respx.post(url).respond(200, json={"ok": True})
        result = await TelegramService.send_notification("TEST_TOKEN", 12345, "Hello")
        assert result is True

    @pytest.mark.asyncio
    @respx.mock
    async def test_send_notification_failure(self):
        url = "https://api.telegram.org/botTEST_TOKEN/sendMessage"
        respx.post(url).respond(400, json={"ok": False})
        result = await TelegramService.send_notification("TEST_TOKEN", 12345, "Hello")
        assert result is False


class TestStartStopCommands:
    """Tests for /start and /stop commands using direct tmux calls."""

    @pytest.fixture
    def bot(self):
        config = BackboneConfig(
            telegram=TelegramConfig(allowed_chat_ids=[100]),
            registry=EntityRegistry(
                entities={
                    "ike": EntityEntry(
                        session="ike",
                        home="~/ws/core/ike",
                        groups=["orchestrators"],
                        figure="Ike",
                        role="Orchestrator",
                    )
                }
            ),
        )
        with patch(
            "agent_backbone.services.telegram.interface.load_discovery",
            return_value=TopicDiscovery(),
        ):
            return TelegramService(config)

    def _make_update(self, chat_id=100):
        update = MagicMock()
        update.effective_chat = MagicMock()
        update.effective_chat.id = chat_id
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()
        return update

    @pytest.mark.asyncio
    async def test_start_agent_calls_start_session(self, bot):
        """The /start command calls start_session with resolved dir."""
        update = self._make_update()
        context = MagicMock()
        context.args = ["ike"]

        with patch(
            "agent_backbone.services.telegram._commands.resolve_agent_dir",
            return_value="/home/test/ws/core/ike",
        ):
            with patch(
                "agent_backbone.services.telegram._commands.start_session",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_start:
                await bot.cmd_start_agent(update, context)

        mock_start.assert_called_once_with(
            "ike",
            working_dir="/home/test/ws/core/ike",
            command=["claude"],
            environment={RUNTIME_ENV_KEY: "claude"},
        )
        reply_text = update.message.reply_text.call_args[0][0]
        assert "Started" in reply_text

    @pytest.mark.asyncio
    async def test_start_agent_unknown(self, bot):
        """The /start command reports error for unknown agent."""
        update = self._make_update()
        context = MagicMock()
        context.args = ["nonexistent"]

        with patch(
            "agent_backbone.services.telegram._commands.resolve_agent_dir",
            return_value="",
        ):
            await bot.cmd_start_agent(update, context)

        reply_text = update.message.reply_text.call_args[0][0]
        assert "Unknown" in reply_text

    @pytest.mark.asyncio
    async def test_stop_agent_calls_stop_session(self, bot):
        """The /stop command calls stop_session directly."""
        update = self._make_update()
        context = MagicMock()
        context.args = ["ike"]

        with patch(
            "agent_backbone.services.telegram._commands.stop_session",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_stop:
            await bot.cmd_stop_agent(update, context)

        mock_stop.assert_called_once_with("ike")
        reply_text = update.message.reply_text.call_args[0][0]
        assert "Stopped" in reply_text

    @pytest.mark.asyncio
    async def test_start_no_args(self, bot):
        """The /start command with no args shows usage."""
        update = self._make_update()
        context = MagicMock()
        context.args = []

        await bot.cmd_start_agent(update, context)
        reply_text = update.message.reply_text.call_args[0][0]
        assert "Usage" in reply_text

    @pytest.mark.asyncio
    async def test_stop_no_args(self, bot):
        """The /stop command with no args shows usage."""
        update = self._make_update()
        context = MagicMock()
        context.args = []

        await bot.cmd_stop_agent(update, context)
        reply_text = update.message.reply_text.call_args[0][0]
        assert "Usage" in reply_text

    @pytest.mark.asyncio
    async def test_start_unauthorized(self, bot):
        """The /start command from unauthorized chat is ignored."""
        update = self._make_update(chat_id=999)
        context = MagicMock()
        context.args = ["ike"]

        await bot.cmd_start_agent(update, context)
        update.message.reply_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop_unauthorized(self, bot):
        """The /stop command from unauthorized chat is ignored."""
        update = self._make_update(chat_id=999)
        context = MagicMock()
        context.args = ["ike"]

        await bot.cmd_stop_agent(update, context)
        update.message.reply_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_start_agent_failure(self, bot):
        """The /start command reports failure when start_session returns False."""
        update = self._make_update()
        context = MagicMock()
        context.args = ["ike"]

        with patch(
            "agent_backbone.services.telegram._commands.resolve_agent_dir",
            return_value="/home/test/ws/core/ike",
        ):
            with patch(
                "agent_backbone.services.telegram._commands.start_session",
                new_callable=AsyncMock,
                return_value=False,
            ):
                await bot.cmd_start_agent(update, context)

        reply_text = update.message.reply_text.call_args[0][0]
        assert "Failed" in reply_text

    @pytest.mark.asyncio
    async def test_stop_agent_failure(self, bot):
        """The /stop command reports failure when stop_session returns False."""
        update = self._make_update()
        context = MagicMock()
        context.args = ["ike"]

        with patch(
            "agent_backbone.services.telegram._commands.stop_session",
            new_callable=AsyncMock,
            return_value=False,
        ):
            await bot.cmd_stop_agent(update, context)

        reply_text = update.message.reply_text.call_args[0][0]
        assert "Failed" in reply_text
