"""Telegram topic message routing."""

from __future__ import annotations

from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import ContextTypes

if TYPE_CHECKING:
    from agent_backbone.services.telegram.interface import TelegramService

from agent_backbone.services.routing import safe_deliver
from agent_backbone.services.telegram._topic_discovery import (
    CATCH_ALL_TOPIC,
    process_message_for_discovery,
)


def _delivery_reply(agent: str, status: str) -> str:
    """Map safe_deliver outcome to a user-friendly Telegram reply."""
    if status == "delivered":
        return f"Sent to `{agent}`."
    if status == "offline":
        return f"`{agent}` is offline."
    if status == "agent_working":
        return f"`{agent}` is busy — queued."
    if status == "waiting_for_human":
        return f"`{agent}` is waiting for a human — queued."
    if status in ("human_typing", "settling"):
        return f"`{agent}` has someone at the keyboard — queued."
    return f"Not delivered to `{agent}` ({status})."


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
        bot._config,
        bot._discovery,
        bot._config.telegram_topic_discovery_path,
    )

    routes = bot._effective_routes()
    target = routes.get(thread_id)
    if target is None:
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
        agent, message, bot._config, db=bot._db, delivery_kind="direct_message"
    )
    await update.message.reply_text(_delivery_reply(agent, result), parse_mode="Markdown")
