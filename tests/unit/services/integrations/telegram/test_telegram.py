"""Tests for the Telegram bot service."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_backbone.config import SecurityConfig, TelegramConfig
from agent_backbone.models import DeliveryOutcome
from agent_backbone.services.agents import AgentState, StartResult, StateSnapshot
from agent_backbone.services.integrations.telegram import TelegramService
from agent_backbone.services.integrations.telegram._routing import _delivery_reply
from agent_backbone.services.integrations.telegram._topic_discovery import CATCH_ALL_TOPIC
from agent_backbone.services.integrations.telegram.interface import _send
from agent_backbone.services.routing import DeliveryReport

_CMD = "agent_backbone.services.integrations.telegram._commands"
_ROUTING = "agent_backbone.services.integrations.telegram._routing"

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


_WAITING_TS = 1_725_400_000.5
_PERMISSION = StateSnapshot(
    state=AgentState.WAITING_FOR_HUMAN, reason="permission", timestamp=_WAITING_TS, source="push"
)
_PLAN = StateSnapshot(
    state=AgentState.WAITING_FOR_HUMAN,
    reason="plan",
    plan_file="/p.md",
    timestamp=_WAITING_TS,
    source="push",
)
_REF = f"{_WAITING_TS:.3f}"


def _callback(data: str, chat_id: int = ALLOWED_CHAT):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.first_name = "Alice"
    update.effective_user.id = 4242
    update.callback_query.data = data
    update.callback_query.message.text = "🔐 Permission prompt — ike"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    return update


class TestButtons:
    def test_inline_keyboard_is_one_row(self):
        from agent_backbone.services.integrations.telegram.interface import inline_keyboard

        markup = inline_keyboard([("Allow", "approve:ike"), ("Deny", "deny:ike")])
        assert markup == {
            "inline_keyboard": [
                [
                    {"text": "Allow", "callback_data": "approve:ike"},
                    {"text": "Deny", "callback_data": "deny:ike"},
                ]
            ]
        }
        assert inline_keyboard(None) is None

    async def test_send_attaches_the_keyboard(self):
        with patch("httpx.AsyncClient") as client:
            post = client.return_value.__aenter__.return_value.post
            post.return_value = MagicMock(status_code=200)
            await _send("tok", 5, "hi", thread_id=7, actions=[("Allow", "approve:ike")])
        payload = post.await_args.kwargs["json"]
        assert payload["message_thread_id"] == 7
        assert payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "approve:ike"

    async def test_allow_answers_the_dialog_and_records_the_user_id(self, config):
        bot = _bot(config)
        update = _callback(f"approve:ike:{_REF}")
        with (
            patch(f"{_CMD}.read_state_file", return_value=_PERMISSION),
            patch(
                f"{_CMD}.approve_agent",
                new_callable=AsyncMock,
                return_value=("approved", ["sent Enter to claude; dialog cleared", "Bash: ls"]),
            ) as approve,
            patch(f"{_CMD}.record_answer", new_callable=AsyncMock) as record,
        ):
            await bot.on_callback(update, _context([]))
        approve.assert_awaited_once_with("ike", runtime="claude")
        assert record.await_args.kwargs["by"] == "telegram:4242"  # the id, not the name
        assert record.await_args.kwargs["verb"] == "approved"
        edited = update.callback_query.edit_message_text.await_args.args[0]
        assert edited.startswith("🔐 Permission prompt — ike")
        assert "Allowed by Alice (telegram:4242)" in edited

    async def test_deny_uses_the_refusing_key(self, config):
        bot = _bot(config)
        update = _callback(f"deny:ike:{_REF}")
        with (
            patch(f"{_CMD}.read_state_file", return_value=_PERMISSION),
            patch(
                f"{_CMD}.deny_agent",
                new_callable=AsyncMock,
                return_value=("denied", ["sent Escape to claude; dialog cleared"]),
            ) as deny,
            patch(f"{_CMD}.record_answer", new_callable=AsyncMock) as record,
        ):
            await bot.on_callback(update, _context([]))
        deny.assert_awaited_once_with("ike", runtime="claude")
        assert record.await_args.kwargs["verb"] == "denied"

    async def test_a_button_for_an_earlier_prompt_answers_nothing(self, config):
        """The agent moved on to a new prompt (a new timestamp): the old button is inert."""
        bot = _bot(config)
        later = replace(_PERMISSION, timestamp=_WAITING_TS + 60)
        update = _callback(f"approve:ike:{_REF}")
        with (
            patch(f"{_CMD}.read_state_file", return_value=later),
            patch(f"{_CMD}.approve_agent", new_callable=AsyncMock) as approve,
        ):
            await bot.on_callback(update, _context([]))
        approve.assert_not_called()
        edited = update.callback_query.edit_message_text.await_args.args[0]
        assert "no longer waiting" in edited and "pressed by Alice (telegram:4242)" in edited

    async def test_a_permission_button_cannot_answer_a_plan(self, config):
        bot = _bot(config)
        update = _callback(f"approve:ike:{_REF}")
        with (
            patch(f"{_CMD}.read_state_file", return_value=_PLAN),
            patch(f"{_CMD}.approve_agent", new_callable=AsyncMock) as approve,
        ):
            await bot.on_callback(update, _context([]))
        approve.assert_not_called()

    async def test_remote_approval_off_refuses_the_button(self, config):
        bot = _bot(config)
        cfg = replace(bot.config, security=SecurityConfig(allow_remote_approval=False))
        bot._config_provider = lambda: cfg
        update = _callback(f"approve:ike:{_REF}")
        with (
            patch(f"{_CMD}.read_state_file", return_value=_PERMISSION),
            patch(f"{_CMD}.approve_agent", new_callable=AsyncMock) as approve,
        ):
            await bot.on_callback(update, _context([]))
        approve.assert_not_called()
        assert "off" in update.callback_query.answer.await_args.args[0]

    async def test_a_dialog_that_vanished_is_reported_with_who_pressed(self, config):
        bot = _bot(config)
        update = _callback(f"approve:ike:{_REF}")
        with (
            patch(f"{_CMD}.read_state_file", return_value=_PERMISSION),
            patch(
                f"{_CMD}.approve_agent",
                new_callable=AsyncMock,
                return_value=("not_waiting", ["terminal shows no active permission prompt:"]),
            ),
            patch(f"{_CMD}.record_answer", new_callable=AsyncMock) as record,
        ):
            await bot.on_callback(update, _context([]))
        record.assert_not_called()
        edited = update.callback_query.edit_message_text.await_args.args[0]
        assert "not_waiting" in edited and "pressed by Alice (telegram:4242)" in edited

    async def test_plan_buttons_need_plan_control(self, config):
        bot = _bot(config)
        update = _callback(f"plan_approve:ike:{_REF}")
        with (
            patch(f"{_CMD}.read_state_file", return_value=_PLAN),
            patch(f"{_CMD}.plan_control", new_callable=AsyncMock) as plan,
        ):
            await bot.on_callback(update, _context([]))
        plan.assert_not_called()
        cfg = replace(bot.config, security=SecurityConfig(allow_remote_plan_control=True))
        bot._config_provider = lambda: cfg
        with (
            patch(f"{_CMD}.read_state_file", return_value=_PLAN),
            patch(
                f"{_CMD}.plan_control",
                new_callable=AsyncMock,
                return_value=("rejected", ["sent Escape"]),
            ) as plan,
        ):
            await bot.on_callback(_callback(f"plan_reject:ike:{_REF}"), _context([]))
        plan.assert_awaited_once_with("ike", "reject", runtime="claude")

    async def test_unauthorized_chat_unknown_agent_and_malformed_data(self, config):
        bot = _bot(config)
        stranger = _callback(f"approve:ike:{_REF}", chat_id=999)
        await bot.on_callback(stranger, _context([]))
        assert "Not allowed" in stranger.callback_query.answer.await_args.args[0]
        unknown = _callback(f"approve:nobody:{_REF}")
        await bot.on_callback(unknown, _context([]))
        assert "Unknown agent" in unknown.callback_query.answer.await_args.args[0]
        old_style = _callback("approve:ike")  # no prompt identity: never acted on
        await bot.on_callback(old_style, _context([]))
        assert "Unknown button" in old_style.callback_query.answer.await_args.args[0]


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
        with patch(
            f"{_CMD}.deliver",
            new_callable=AsyncMock,
            return_value=DeliveryReport(DeliveryOutcome.DELIVERED),
        ) as d:
            await bot.cmd_tell(update, _context(["ike", "hello", "there"]))
        d.assert_awaited_once()
        assert d.await_args.args[:2] == ("ike", "[via:telegram from:alice] hello there")
        assert d.await_args.kwargs["delivery_kind"] == "direct_message"
        update.message.reply_text.assert_awaited_once_with("Sent to `ike`.", parse_mode="Markdown")

    async def test_cmd_tell_busy(self, config):
        bot = _bot(config)
        update = _update()
        with patch(
            f"{_CMD}.deliver",
            new_callable=AsyncMock,
            return_value=DeliveryReport(DeliveryOutcome.AGENT_WORKING, "stored"),
        ):
            await bot.cmd_tell(update, _context(["ike", "hi"]))
        update.message.reply_text.assert_awaited_once_with(
            "`ike` is busy — queued.", parse_mode="Markdown"
        )

    async def test_cmd_tell_same_message_already_waiting(self, config):
        bot = _bot(config)
        update = _update()
        with patch(
            f"{_CMD}.deliver",
            new_callable=AsyncMock,
            return_value=DeliveryReport(DeliveryOutcome.AGENT_WORKING, "already_queued"),
        ) as d:
            await bot.cmd_tell(update, _context(["ike", "hi"]))
        assert d.await_args.kwargs["sender"] == "alice"
        update.message.reply_text.assert_awaited_once_with(
            "The same message from you is already in the queue, waiting for `ike`.",
            parse_mode="Markdown",
        )

    async def test_cmd_tell_refuses_unregistered_session(self, config):
        bot = _bot(config)
        update = _update()
        with patch(f"{_CMD}.deliver", new_callable=AsyncMock) as deliver:
            await bot.cmd_tell(update, _context(["stray-tmux", "hi"]))
        deliver.assert_not_awaited()
        assert "Unknown agent" in update.message.reply_text.await_args.args[0]

    async def test_unauthorized_ignored(self, config):
        bot = _bot(config)
        update = _update(chat_id=999)
        with patch(f"{_CMD}.deliver", new_callable=AsyncMock) as d:
            await bot.cmd_tell(update, _context(["ike", "hi"]))
        d.assert_not_called()
        update.message.reply_text.assert_not_awaited()


class TestTopicRouting:
    async def test_direct_topic_to_mapped_agent(self, config):
        bot = _bot(config, topic_routes={42: "ike"})
        update = _update("do the thing", thread_id=42)
        with patch(
            f"{_ROUTING}.deliver",
            new_callable=AsyncMock,
            return_value=DeliveryReport(DeliveryOutcome.DELIVERED),
        ) as d:
            await bot.handle_topic_message(update, MagicMock())
        assert d.await_args.args[:2] == ("ike", "[via:telegram from:alice] do the thing")

    async def test_catch_all_topic_parses_agent_prefix(self, config):
        bot = _bot(config, topic_routes={43: CATCH_ALL_TOPIC})
        update = _update("feynman: run tests", thread_id=43)
        with patch(
            f"{_ROUTING}.deliver",
            new_callable=AsyncMock,
            return_value=DeliveryReport(DeliveryOutcome.OFFLINE, "stored"),
        ) as d:
            await bot.handle_topic_message(update, MagicMock())
        assert d.await_args.args[0] == "feynman"
        update.message.reply_text.assert_awaited_once_with(
            "`feynman` is offline — queued.", parse_mode="Markdown"
        )

    async def test_catch_all_topic_without_body(self, config):
        bot = _bot(config, topic_routes={43: CATCH_ALL_TOPIC})
        update = _update("feynman", thread_id=43)
        with patch(f"{_ROUTING}.deliver", new_callable=AsyncMock) as d:
            await bot.handle_topic_message(update, MagicMock())
        d.assert_not_called()
        assert "Usage" in update.message.reply_text.await_args.args[0]

    async def test_catch_all_topic_refuses_unregistered_session(self, config):
        bot = _bot(config, topic_routes={43: CATCH_ALL_TOPIC})
        update = _update("stray-tmux: run tests", thread_id=43)
        with patch(f"{_ROUTING}.deliver", new_callable=AsyncMock) as deliver:
            await bot.handle_topic_message(update, MagicMock())
        deliver.assert_not_awaited()
        assert "Unknown agent" in update.message.reply_text.await_args.args[0]

    async def test_unmapped_topic_ignored(self, config):
        bot = _bot(config)
        with patch(f"{_ROUTING}.deliver", new_callable=AsyncMock) as d:
            await bot.handle_topic_message(_update("x", thread_id=99), MagicMock())
        d.assert_not_called()


class TestStartStop:
    async def test_start_configured_agent(self, config):
        bot = _bot(config)
        update = _update()
        with patch(
            f"{_CMD}.start_agent", new_callable=AsyncMock, return_value=StartResult(ok=True)
        ) as start:
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
        with patch(f"{_CMD}.stop_agent_session", new_callable=AsyncMock, return_value=True):
            await bot.cmd_stop_agent(update, _context(["ike"]))
        update.message.reply_text.assert_awaited_once_with("Stopped `ike`", parse_mode="Markdown")

    async def test_stop_refuses_unregistered_session(self, config):
        bot = _bot(config)
        update = _update()
        with patch(f"{_CMD}.stop_agent_session", new_callable=AsyncMock) as stop:
            await bot.cmd_stop_agent(update, _context(["stray-tmux"]))
        stop.assert_not_awaited()
        assert "Unknown agent" in update.message.reply_text.await_args.args[0]

    async def test_stop_refuses_backbone_session(self, config):
        bot = _bot(config)
        update = _update()
        with patch(
            "agent_backbone.services.agents.operations.launch.stop_agent", new_callable=AsyncMock
        ) as stop:
            await bot.cmd_stop_agent(update, _context([config.backbone.session_name]))
        stop.assert_not_awaited()
        assert "Refusing" in update.message.reply_text.await_args.args[0]


class TestPlans:
    async def test_approve_disabled_by_default(self, config):
        bot = _bot(config)
        update = _update()
        with patch(f"{_CMD}.plan_control", new_callable=AsyncMock) as approve:
            await bot.cmd_approve(update, _context(["ike"]))
        approve.assert_not_called()
        assert "disabled" in update.message.reply_text.await_args.args[0]

    async def test_approve_refuses_unregistered_session(self, config):
        telegram = TelegramConfig(allowed_chat_ids=(ALLOWED_CHAT,))
        cfg = replace(
            config, telegram=telegram, security=SecurityConfig(allow_remote_plan_control=True)
        )
        bot = TelegramService(cfg, db=AsyncMock())
        update = _update()
        with patch(f"{_CMD}.plan_control", new_callable=AsyncMock) as approve:
            await bot.cmd_approve(update, _context(["stray-tmux"]))
        approve.assert_not_awaited()
        assert "Unknown agent" in update.message.reply_text.await_args.args[0]

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
            patch(
                f"{_CMD}.plan_control",
                new_callable=AsyncMock,
                return_value=("approved", ["sent Escape [Z to claude"]),
            ) as approve,
        ):
            await bot.cmd_approve(update, _context(["ike"]))
        approve.assert_awaited_once_with("ike", "approve", runtime="claude")
        assert "approved" in update.message.reply_text.await_args.args[0]

    async def test_approve_on_a_runtime_without_plan_mode_says_so(self, config):
        telegram = TelegramConfig(allowed_chat_ids=(ALLOWED_CHAT,))
        cfg = replace(
            config, telegram=telegram, security=SecurityConfig(allow_remote_plan_control=True)
        )
        bot = TelegramService(cfg, db=AsyncMock())
        update = _update()
        snapshot = StateSnapshot(
            state=AgentState.WAITING_FOR_HUMAN, reason="plan", plan_file="/p.md"
        )
        refusal = ("unsupported", ["Codex has no plan mode; nothing was sent"])
        with (
            patch(f"{_CMD}.read_state_file", return_value=snapshot),
            patch(f"{_CMD}.session_exists", new_callable=AsyncMock, return_value=True),
            patch(f"{_CMD}.plan_control", new_callable=AsyncMock, return_value=refusal),
        ):
            await bot.cmd_approve(update, _context(["ike"]))
        assert "not available" in update.message.reply_text.await_args.args[0]

    async def test_viewplan_shows_content(self, config):
        bot = _bot(config)
        update = _update()
        plans_dir = config.state_dir / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        plan = plans_dir / "ike.md"
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

    async def test_viewplan_refuses_a_plan_outside_the_state_dir(self, config, tmp_path):
        # The path comes from the agent-writable state file: never read elsewhere.
        bot = _bot(config)
        update = _update()
        secret = tmp_path / "secret.txt"
        secret.write_text("hunter2")
        snapshot = StateSnapshot(
            state=AgentState.WAITING_FOR_HUMAN, reason="plan", plan_file=str(secret)
        )
        with patch(f"{_CMD}.read_state_file", return_value=snapshot):
            await bot.cmd_viewplan(update, _context(["ike"]))
        text = update.message.reply_text.await_args.args[0]
        assert "hunter2" not in text and "no readable plan" in text


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
        bot._db.deliveries.query.return_value = [
            {"issue_number": 1, "target_entity": "ike", "outcome": "delivered"}
        ]
        bot._db.deliveries.failed.return_value = []
        update = _update()
        await bot.cmd_queue(update, MagicMock())
        assert "Recent Deliveries" in update.message.reply_text.await_args.args[0]


class TestDeliveryReplyFallbacks:
    @pytest.mark.parametrize(
        ("status", "queue", "expected"),
        [
            (DeliveryOutcome.DELIVERED, None, "Sent to `ike`."),
            (DeliveryOutcome.OFFLINE, "stored", "`ike` is offline — queued."),
            (DeliveryOutcome.OFFLINE, None, "`ike` is offline."),
            (DeliveryOutcome.WAITING_FOR_HUMAN, "stored", "`ike` is waiting for a human — queued."),
            (DeliveryOutcome.HUMAN_TYPING, "stored", "`ike` has someone at the keyboard — queued."),
            (
                DeliveryOutcome.AGENT_WORKING,
                "already_queued",
                "The same message from you is already in the queue, waiting for `ike`.",
            ),
            (
                DeliveryOutcome.AGENT_WORKING,
                "failed",
                "Not delivered and not queued: could not store the message for `ike`.",
            ),
            (DeliveryOutcome.DELIVERY_FAILED, None, "Not delivered to `ike` (delivery_failed)."),
        ],
    )
    def test_reply(self, status, queue, expected):
        assert _delivery_reply("ike", DeliveryReport(status, queue)) == expected


class TestSend:
    async def test_send_success(self):
        response = MagicMock(status_code=200)
        client = AsyncMock()
        client.post = AsyncMock(return_value=response)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        with patch(
            "agent_backbone.services.integrations.telegram.interface.httpx.AsyncClient",
            return_value=client,
        ):
            assert await _send("tok", 1, "hi") is True

    async def test_send_failure(self):
        client = AsyncMock()
        client.post = AsyncMock(return_value=MagicMock(status_code=400, text="bad"))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        with patch(
            "agent_backbone.services.integrations.telegram.interface.httpx.AsyncClient",
            return_value=client,
        ):
            assert await _send("tok", 1, "hi") is False


class TestGeneralAndUnmapped:
    async def test_general_text_gets_a_pointer_not_a_guess(self, config):
        from agent_backbone.services.integrations.telegram import _routing

        _routing._hinted.clear()
        bot = _bot(config)
        update = _update("ike: run the tests", thread_id=None)
        with patch(f"{_ROUTING}.deliver", new_callable=AsyncMock) as d:
            await bot.handle_general_message(update, MagicMock())
            await bot.handle_general_message(update, MagicMock())  # deduped
        d.assert_not_called()
        update.message.reply_text.assert_awaited_once()
        assert "own topic" in update.message.reply_text.await_args.args[0]

    async def test_general_message_teaches_the_group_id_and_triggers_a_sync(self, config):
        bot = _bot(config)
        assert bot._effective_group_chat_id() is None
        update = _update("hello", chat_id=ALLOWED_CHAT, thread_id=None)
        with patch.object(bot, "sync_agents", new_callable=AsyncMock) as sync:
            await bot.handle_general_message(update, MagicMock())
            for task in list(bot._background):
                await task
        assert bot._effective_group_chat_id() == ALLOWED_CHAT
        sync.assert_awaited_once()

    async def test_unmapped_topic_gets_a_hint_once(self, config):
        from agent_backbone.services.integrations.telegram import _routing

        _routing._hinted.clear()
        bot = _bot(config)
        update = _update("x", thread_id=99)
        with patch(f"{_ROUTING}.deliver", new_callable=AsyncMock) as d:
            await bot.handle_topic_message(update, MagicMock())
            await bot.handle_topic_message(update, MagicMock())
        d.assert_not_called()
        update.message.reply_text.assert_awaited_once()
        assert "not an agent's" in update.message.reply_text.await_args.args[0]


class TestDiscoveryAuthorization:
    async def test_unauthorized_group_teaches_nothing_and_syncs_nothing(self, config):
        # A stranger's supergroup must never become "the" group and get topics.
        bot = _bot(config)
        update = _update("hello", chat_id=-999, thread_id=None)
        with patch.object(bot, "sync_agents", new_callable=AsyncMock) as sync:
            await bot.handle_general_message(update, MagicMock())
            await bot.cmd_status(update, _context([]))
        assert bot._effective_group_chat_id() is None
        sync.assert_not_called()
        update.message.reply_text.assert_not_awaited()
