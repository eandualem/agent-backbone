"""Telegram message routing: an agent's topic is that agent; General is the lobby."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import ContextTypes

if TYPE_CHECKING:
    from agent_backbone.services.integrations.telegram.interface import TelegramService

from agent_backbone.services.integrations.telegram._topic_discovery import (
    CATCH_ALL_TOPIC,
    process_message_for_discovery,
)
from agent_backbone.services.routing import safe_deliver

_HINT_DEDUP_SECONDS = 300
_hinted_at: dict[tuple[int, int | None], float] = {}

GENERAL_HINT = (
    "Each agent has its own topic here — write in an agent's topic to talk to it.\n"
    "In General: /status, /start <agent>, /tell <agent> <text>, /help"
)
UNMAPPED_HINT = (
    "This topic is not an agent's. Agents' topics are created automatically; "
    "run /identify here to see this topic's id if you want to map it by hand."
)


def _hint_due(chat_id: int, thread_id: int | None) -> bool:
    """Once per chat/topic per ``_HINT_DEDUP_SECONDS`` — guidance, not noise."""
    key = (chat_id, thread_id)
    now = time.monotonic()
    last = _hinted_at.get(key)
    if last is not None and now - last < _HINT_DEDUP_SECONDS:
        return False
    _hinted_at[key] = now
    return True


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
        agent, message, bot._config, db=bot._db, delivery_kind="direct_message"
    )
    await update.message.reply_text(_delivery_reply(agent, result), parse_mode="Markdown")
