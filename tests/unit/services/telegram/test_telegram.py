"""Tests for the Telegram bot service."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_backbone.config import SecurityConfig, TelegramConfig
from agent_backbone.services.agents import AgentState, StateSnapshot
from agent_backbone.services.telegram import TelegramService
from agent_backbone.services.telegram._routing import _delivery_reply
from agent_backbone.services.telegram._topic_discovery import CATCH_ALL_TOPIC

_CMD = "agent_backbone.services.telegram._commands"
_ROUTING = "agent_backbone.services.telegram._routing"

ALLOWED_CHAT = 111


def _bot(config, **overrides) -> TelegramService:
    telegram = TelegramConfig(allowed_chat_ids=(ALLOWED_CHAT,), **overrides)
    return TelegramService(replace(config, telegram=telegram), db=AsyncMock())


def _update(text: str = "", chat_id: int = ALLOWED_CHAT, thread_id: int | None = None):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.first_name = "Alice"
    update.message.text = text
    update.message.message_thread_id = thread_id
    update.message.chat.type = "supergroup"
    update.message.chat.id = chat_id
    update.message.reply_to_message = None
    update.message.reply_text = AsyncMock()
    return update


def _context(args: list[str]):
    ctx = MagicMock()
    ctx.args = args
    return ctx


class TestAuthorization:
    def test_empty_allowlist_rejects_everyone(self, config):
        bot = TelegramService(config)
        assert bot._is_authorized(ALLOWED_CHAT) is False

    def test_allowlist_enforced(self, config):
        bot = _bot(config)
        assert bot._is_authorized(ALLOWED_CHAT) is True
        assert bot._is_authorized(222) is False

    async def test_start_refuses_without_allowlist(self, config):
        bot = TelegramService(replace(config, telegram_token="t"))
        with patch.object(bot, "build_app") as build:
            await bot.start()
        build.assert_not_called()
        assert bot.running is False

    async def test_start_noop_without_token(self, config):
        bot = _bot(config)
        await bot.start()
        assert bot.running is False
        assert (await bot.health_check())["healthy"] is True


class TestTell:
    async def test_cmd_tell_uses_safe_deliver(self, config):
        bot = _bot(config)
        update = _update()
        with patch(f"{_CMD}.safe_deliver", new_callable=AsyncMock, return_value="delivered") as d:
            await bot.cmd_tell(update, _context(["ike", "hello", "there"]))
        d.assert_awaited_once()
        assert d.await_args.args[:2] == ("ike", "[via:telegram from:alice] hello there")
        assert d.await_args.kwargs["delivery_kind"] == "direct_message"
        update.message.reply_text.assert_awaited_once_with("Sent to `ike`.", parse_mode="Markdown")

    async def test_cmd_tell_busy(self, config):
        bot = _bot(config)
        update = _update()
        with patch(f"{_CMD}.safe_deliver", new_callable=AsyncMock, return_value="agent_working"):
            await bot.cmd_tell(update, _context(["ike", "hi"]))
        update.message.reply_text.assert_awaited_once_with(
            "`ike` is busy — queued.", parse_mode="Markdown"
        )

    async def test_unauthorized_ignored(self, config):
        bot = _bot(config)
        update = _update(chat_id=999)
        with patch(f"{_CMD}.safe_deliver", new_callable=AsyncMock) as d:
            await bot.cmd_tell(update, _context(["ike", "hi"]))
        d.assert_not_called()
        update.message.reply_text.assert_not_awaited()


class TestTopicRouting:
    async def test_direct_topic_to_mapped_agent(self, config):
        bot = _bot(config, topic_routes={42: "ike"})
        update = _update("do the thing", thread_id=42)
        with patch(
            f"{_ROUTING}.safe_deliver", new_callable=AsyncMock, return_value="delivered"
        ) as d:
            await bot.handle_topic_message(update, MagicMock())
        assert d.await_args.args[:2] == ("ike", "[via:telegram from:alice] do the thing")

    async def test_catch_all_topic_parses_agent_prefix(self, config):
        bot = _bot(config, topic_routes={43: CATCH_ALL_TOPIC})
        update = _update("feynman: run tests", thread_id=43)
        with patch(f"{_ROUTING}.safe_deliver", new_callable=AsyncMock, return_value="offline") as d:
            await bot.handle_topic_message(update, MagicMock())
        assert d.await_args.args[0] == "feynman"
        update.message.reply_text.assert_awaited_once_with(
            "`feynman` is offline.", parse_mode="Markdown"
        )

    async def test_catch_all_topic_without_body(self, config):
        bot = _bot(config, topic_routes={43: CATCH_ALL_TOPIC})
        update = _update("feynman", thread_id=43)
        with patch(f"{_ROUTING}.safe_deliver", new_callable=AsyncMock) as d:
            await bot.handle_topic_message(update, MagicMock())
        d.assert_not_called()
        assert "Usage" in update.message.reply_text.await_args.args[0]

    async def test_unmapped_topic_ignored(self, config):
        bot = _bot(config)
        with patch(f"{_ROUTING}.safe_deliver", new_callable=AsyncMock) as d:
            await bot.handle_topic_message(_update("x", thread_id=99), MagicMock())
        d.assert_not_called()


class TestStartStop:
    async def test_start_configured_agent(self, config):
        bot = _bot(config)
        update = _update()
        with patch(f"{_CMD}.start_agent", new_callable=AsyncMock, return_value=True) as start:
            await bot.cmd_start_agent(update, _context(["ike"]))
        assert start.await_args.args[0].name == "ike"
        update.message.reply_text.assert_awaited_once_with("Started `ike`", parse_mode="Markdown")

    async def test_start_unknown_agent(self, config):
        bot = _bot(config)
        update = _update()
        with patch(f"{_CMD}.start_agent", new_callable=AsyncMock) as start:
            await bot.cmd_start_agent(update, _context(["nobody"]))
        start.assert_not_called()
        assert "Unknown agent" in update.message.reply_text.await_args.args[0]

    async def test_stop_agent(self, config):
        bot = _bot(config)
        update = _update()
        with patch(f"{_CMD}.stop_agent", new_callable=AsyncMock, return_value=True):
            await bot.cmd_stop_agent(update, _context(["ike"]))
        update.message.reply_text.assert_awaited_once_with("Stopped `ike`", parse_mode="Markdown")


class TestPlans:
    async def test_approve_disabled_by_default(self, config):
        bot = _bot(config)
        update = _update()
        with patch(f"{_CMD}.send_keys", new_callable=AsyncMock) as keys:
            await bot.cmd_approve(update, _context(["ike"]))
        keys.assert_not_called()
        assert "disabled" in update.message.reply_text.await_args.args[0]

    async def test_approve_sends_keys_when_enabled(self, config):
        telegram = TelegramConfig(allowed_chat_ids=(ALLOWED_CHAT,))
        cfg = replace(
            config, telegram=telegram, security=SecurityConfig(allow_remote_plan_control=True)
        )
        bot = TelegramService(cfg, db=AsyncMock())
        update = _update()
        snapshot = StateSnapshot(
            state=AgentState.WAITING_FOR_HUMAN, reason="plan", plan_file="/p.md"
        )
        with (
            patch(f"{_CMD}.read_state_file", return_value=snapshot),
            patch(f"{_CMD}.session_exists", new_callable=AsyncMock, return_value=True),
            patch(f"{_CMD}.send_keys", new_callable=AsyncMock, return_value=True) as keys,
        ):
            await bot.cmd_approve(update, _context(["ike"]))
        assert [c.args[1] for c in keys.await_args_list] == ["Escape", "[Z"]

    async def test_viewplan_shows_content(self, config, tmp_path):
        bot = _bot(config)
        update = _update()
        plan = tmp_path / "plan.md"
        plan.write_text("# plan body")
        snapshot = StateSnapshot(
            state=AgentState.WAITING_FOR_HUMAN,
            reason="plan",
            plan_file=str(plan),
            plan_title="Big plan",
        )
        with patch(f"{_CMD}.read_state_file", return_value=snapshot):
            await bot.cmd_viewplan(update, _context(["ike"]))
        text = update.message.reply_text.await_args.args[0]
        assert "Big plan" in text and "# plan body" in text


class TestStatusAndQueue:
    async def test_status_marks_running_agents(self, config):
        bot = _bot(config)
        update = _update()
        with patch(f"{_CMD}.list_sessions", new_callable=AsyncMock, return_value=["ike", "x"]):
            await bot.cmd_status(update, MagicMock())
        text = update.message.reply_text.await_args.args[0]
        assert "`ike`" in text and "Other tmux sessions" in text

    async def test_queue_uses_shared_db(self, config):
        bot = _bot(config)
        bot._db.query_deliveries.return_value = [
            {"issue_number": 1, "target_entity": "ike", "outcome": "delivered"}
        ]
        bot._db.get_failed_deliveries.return_value = []
        update = _update()
        await bot.cmd_queue(update, MagicMock())
        assert "Recent Deliveries" in update.message.reply_text.await_args.args[0]


class TestDeliveryReplyFallbacks:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("delivered", "Sent to `ike`."),
            ("offline", "`ike` is offline."),
            ("waiting_for_human", "`ike` is waiting for a human — queued."),
            ("human_typing", "`ike` has someone at the keyboard — queued."),
            ("delivery_failed", "Not delivered to `ike` (delivery_failed)."),
        ],
    )
    def test_reply(self, status, expected):
        assert _delivery_reply("ike", status) == expected


class TestSendNotification:
    async def test_send_notification_success(self):
        response = MagicMock(status_code=200)
        client = AsyncMock()
        client.post = AsyncMock(return_value=response)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        with patch(
            "agent_backbone.services.telegram.interface.httpx.AsyncClient", return_value=client
        ):
            assert await TelegramService.send_notification("tok", 1, "hi") is True

    async def test_send_notification_failure(self):
        client = AsyncMock()
        client.post = AsyncMock(return_value=MagicMock(status_code=400, text="bad"))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        with patch(
            "agent_backbone.services.telegram.interface.httpx.AsyncClient", return_value=client
        ):
            assert await TelegramService.send_notification("tok", 1, "hi") is False
