"""Telegram message routing: an agent's topic is that agent; General is the lobby."""

from __future__ import annotations

from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import ContextTypes

if TYPE_CHECKING:
    from agent_backbone.services.integrations.telegram.interface import TelegramService

from agent_backbone.models import DeliveryOutcome
from agent_backbone.recent import RecentKeys
from agent_backbone.services.integrations.telegram._topic_discovery import (
    CATCH_ALL_TOPIC,
    process_message_for_discovery,
)
from agent_backbone.services.routing import safe_deliver

_hinted = RecentKeys(300)
"""(chat, topic) pairs hinted in the last five minutes — guidance, not noise."""

GENERAL_HINT = (
    "Each agent has its own topic here — write in an agent's topic to talk to it.\n"
    "In General: /status, /start <agent>, /tell <agent> <text>, /help"
)
UNMAPPED_HINT = (
    "This topic is not an agent's. Agents' topics are created automatically; "
    "run /identify here to see this topic's id if you want to map it by hand."
)


def _hint_due(chat_id: int, thread_id: int | None) -> bool:
    return not _hinted.check_and_mark((chat_id, thread_id))


async def handle_general_message(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Plain text in the group's General topic: point at the per-agent topics.

    The old ``agent: text`` guessing is gone on purpose — the group's General
    topic is for commands and orientation; talking to an agent happens in
    its topic. (Discovery already ran in the wrapper, so this message was
    also enough to learn the group id and create the topics.)
    """
    chat = update.effective_chat
    if not chat or not bot._is_authorized(chat.id):
        return
    if not (update.message.text or "").strip():
        return
    if _hint_due(chat.id, None):
        await update.message.reply_text(GENERAL_HINT)


def _delivery_reply(agent: str, outcome: DeliveryOutcome) -> str:
    """Map a delivery outcome to a user-friendly Telegram reply."""
    if outcome == DeliveryOutcome.DELIVERED:
        return f"Sent to `{agent}`."
    if outcome == DeliveryOutcome.OFFLINE:
        return f"`{agent}` is offline."
    if outcome == DeliveryOutcome.AGENT_WORKING:
        return f"`{agent}` is busy — queued."
    if outcome == DeliveryOutcome.WAITING_FOR_HUMAN:
        return f"`{agent}` is waiting for a human — queued."
    if outcome in (DeliveryOutcome.HUMAN_TYPING, DeliveryOutcome.SETTLING):
        return f"`{agent}` has someone at the keyboard — queued."
    return f"Not delivered to `{agent}` ({outcome.value})."


async def handle_topic_message(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Route plain text messages in forum topics to mapped agent sessions."""
    if not update.effective_chat or not bot._is_authorized(update.effective_chat.id):
        return

    thread_id = getattr(update.message, "message_thread_id", None)
    if thread_id is None:
        return

    process_message_for_discovery(
        update,
        bot.config,
        bot._discovery,
        bot.config.telegram_topic_discovery_path,
    )

    routes = bot._effective_routes()
    target = routes.get(thread_id)
    if target is None:
        if (update.message.text or "").strip() and _hint_due(update.effective_chat.id, thread_id):
            await update.message.reply_text(UNMAPPED_HINT)
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    sender = bot._sender_tag(update)
    tag = f"[via:telegram from:{sender}]"

    if target == CATCH_ALL_TOPIC:
        # Parse "agent-name: message" or "agent-name message"
        parts = text.split(":", 1) if ":" in text else text.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            await update.message.reply_text(
                "Usage: `agent-name: message` or `agent-name message`",
                parse_mode="Markdown",
            )
            return
        agent = parts[0].strip()
        message = f"{tag} {parts[1].strip()}"
    else:
        agent = target
        message = f"{tag} {text}"

    result = await safe_deliver(
        agent, message, bot.config, db=bot._db, delivery_kind="direct_message"
    )
    await update.message.reply_text(_delivery_reply(agent, result), parse_mode="Markdown")
