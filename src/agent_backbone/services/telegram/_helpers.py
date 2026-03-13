"""Shared Telegram bot helpers for command and topic handlers."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from telegram import Message, Update

from agent_backbone.services.agents._file_reader import read_state_file
from agent_backbone.services.agents.models import AgentState, StateSnapshot

if TYPE_CHECKING:
    from agent_backbone.services.telegram.interface import TelegramService


def authorized_message(bot: TelegramService, update: Update) -> Message | None:
    """Return the message only when the chat is authorized for bot control."""
    chat = update.effective_chat
    message = update.message
    if chat is None or message is None:
        return None
    if not bot._is_authorized(chat.id):
        return None
    return message


def read_plan_waiting_snapshot(bot: TelegramService, agent: str) -> StateSnapshot | None:
    """Load the agent state snapshot when it is waiting on plan approval."""
    snapshot = read_state_file(bot._config.agent_state.state_path, agent)
    if snapshot is None or snapshot.state != AgentState.PLAN_WAITING:
        return None
    return snapshot


def split_message_chunks(text: str, max_len: int = 4096) -> Iterator[str]:
    """Yield Telegram-safe message chunks without altering the original text."""
    for index in range(0, len(text), max_len):
        yield text[index : index + max_len]
