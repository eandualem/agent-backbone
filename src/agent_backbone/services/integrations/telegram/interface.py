"""Telegram — the first integration (see ``services/integrations/base.py``).

Runs inside the backbone process as a lifecycle component: ``start()``
launches long-polling in the background when ``TELEGRAM_TOKEN`` is set,
``stop()`` shuts it down. Commands live in ``_commands.py``, topic routing in
``_routing.py``, topic discovery in ``_topic_discovery.py``.

An agent's surface on Telegram is a forum topic mapped to it
(``telegram.topic_routes`` or auto-discovered); ``reply_to_agent`` and
``notify(agent=…)`` post there, alerts without a topic go to
``telegram.notification_chat_id``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

import httpx
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from agent_backbone.config import BackboneConfig
from agent_backbone.services.integrations.base import Integration
from agent_backbone.services.integrations.telegram import _commands, _routing
from agent_backbone.services.integrations.telegram._topic_discovery import (
    agent_topic,
    effective_group_chat_id,
    effective_routes,
    load_discovery,
    process_message_for_discovery,
)

if TYPE_CHECKING:
    from agent_backbone.services.database import BackboneDB

log = logging.getLogger(__name__)


def inline_keyboard(actions: list[tuple[str, str]] | None) -> dict | None:
    """Telegram's ``reply_markup`` for ``(label, callback data)`` buttons, one row."""
    if not actions:
        return None
    row = [{"text": label, "callback_data": data} for label, data in actions]
    return {"inline_keyboard": [row]}


async def _send(
    token: str,
    chat_id: int,
    text: str,
    *,
    thread_id: int | None = None,
    actions: list[tuple[str, str]] | None = None,
) -> bool:
    """One sendMessage through the HTTP API (no bot instance needed)."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: dict = {"chat_id": chat_id, "text": text}
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    markup = inline_keyboard(actions)
    if markup is not None:
        payload["reply_markup"] = markup
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                return True
            log.warning("Telegram sendMessage failed: %s %s", resp.status_code, resp.text)
            return False
    except httpx.HTTPError as e:
        log.warning("Telegram sendMessage error: %s", e)
        return False


async def notify_static(
    config: BackboneConfig,
    text: str,
    *,
    agent: str | None = None,
    actions: list[tuple[str, str]] | None = None,
) -> bool:
    """Config-driven alert for callers without the bot instance (scheduler jobs).

    Goes into the agent's topic when it has one and the group is known,
    otherwise to ``telegram.notification_chat_id``. ``actions`` become an
    inline keyboard the bot answers in ``on_callback``. False when Telegram
    is not configured for either destination.
    """
    token = config.telegram_token
    if not token:
        return False
    discovery = load_discovery(config.telegram_topic_discovery_path)
    if agent:
        group = effective_group_chat_id(config, discovery)
        thread_id = agent_topic(config, discovery, agent)
        if group and thread_id is not None:
            return await _send(token, group, text, thread_id=thread_id, actions=actions)
    chat_id = config.telegram.notification_chat_id
    if not chat_id:
        return False
    return await _send(token, chat_id, text, actions=actions)


class TelegramService(Integration):
    """Telegram bot for agent backbone management."""

    name = "telegram"

    def __init__(
        self,
        config: BackboneConfig | Callable[[], BackboneConfig],
        db: BackboneDB | None = None,
    ) -> None:
        super().__init__(config, db=db)
        self._app: Application | None = None
        self._discovery = load_discovery(self.config.telegram_topic_discovery_path)
        self._background: set[asyncio.Task] = set()
        self._sync_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.config.telegram_token)

    # -- Integration contract --

    async def reply_to_agent(self, agent: str, text: str) -> bool:
        """Post an agent's answer into its topic; False when it has none."""
        group = self._effective_group_chat_id()
        thread_id = agent_topic(self.config, self._discovery, agent)
        if not group or thread_id is None:
            return False
        if not await _send(self.config.telegram_token, group, text, thread_id=thread_id):
            # The registry reports False as "no surface"; an outage is "failed".
            raise RuntimeError(f"Telegram could not post to {agent}'s topic")
        return True

    async def notify(
        self,
        text: str,
        *,
        agent: str | None = None,
        actions: list[tuple[str, str]] | None = None,
    ) -> bool:
        return await notify_static(self.config, text, agent=agent, actions=actions)

    async def sync_agents(self) -> None:
        """One forum topic per registered agent (see ``_topics``)."""
        from agent_backbone.services.integrations.telegram._topics import sync_topics

        # Start, config publish, periodic job and first-group discovery can all
        # fire close together; serialized so two syncs never both create a topic.
        async with self._sync_lock:
            await sync_topics(self)

    def _discover(self, update: Update) -> None:
        """Learn the group id / topic names from any message; sync topics on news.

        Every handler runs this first, so the first message in a fresh forum
        group is enough for the bot to know where to create topics. Only
        allow-listed chats teach anything: a stranger's group must never
        become "the" group and receive agent topics.
        """
        chat = getattr(update, "effective_chat", None)
        if chat is None or not self._is_authorized(chat.id):
            return
        knew_group = self._effective_group_chat_id() is not None
        changed = process_message_for_discovery(
            update, self.config, self._discovery, self.config.telegram_topic_discovery_path
        )
        if changed and not knew_group and self._effective_group_chat_id() is not None:
            task = asyncio.get_running_loop().create_task(self.sync_agents())
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    def _effective_routes(self) -> dict[int, str]:
        """Merged routes: discovery + config (config wins)."""
        return effective_routes(self.config, self._discovery)

    def _effective_group_chat_id(self) -> int | None:
        """Group chat ID: config wins over discovery."""
        return effective_group_chat_id(self.config, self._discovery)

    @staticmethod
    def _sender_tag(update: Update) -> str:
        """Readable sender name for the [via:telegram from:X] envelope."""
        user = getattr(update, "effective_user", None)
        if user:
            return (user.first_name or user.username or "unknown").lower()
        return "unknown"

    @staticmethod
    def _sender_id(update: Update) -> str:
        """Stable queue identity for the Telegram user: ``telegram:<id>``.

        First names collide — two users named Alice were one sender under
        the display tag, so one's text deduplicated the other's. The user
        id does not collide; the envelope keeps the readable name.
        """
        user = getattr(update, "effective_user", None)
        user_id = getattr(user, "id", None) if user is not None else None
        if not isinstance(user_id, int):
            return "telegram:unknown"
        return f"telegram:{user_id}"

    def _is_authorized(self, chat_id: int) -> bool:
        """Only chats on the allowlist may control the backbone."""
        return chat_id in self.config.telegram.allowed_chat_ids

    # -- Command handler thin wrappers (delegate to _commands module) --

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._discover(update)
        await _commands.cmd_help(self, update, context)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._discover(update)
        await _commands.cmd_status(self, update, context)

    async def cmd_queue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _commands.cmd_queue(self, update, context)

    async def cmd_start_agent(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _commands.cmd_start_agent(self, update, context)

    async def cmd_stop_agent(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _commands.cmd_stop_agent(self, update, context)

    async def cmd_tell(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _commands.cmd_tell(self, update, context)

    async def cmd_digest(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _commands.cmd_digest(self, update, context)

    async def cmd_identify(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._discover(update)
        await _commands.cmd_identify(self, update, context)

    async def cmd_viewplan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _commands.cmd_viewplan(self, update, context)

    async def cmd_approve(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _commands.cmd_approve(self, update, context)

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """A button on an alert: Allow / Deny a permission prompt, Approve / Reject a plan."""
        await _commands.on_callback(self, update, context)

    async def handle_topic_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        self._discover(update)
        await _routing.handle_topic_message(self, update, context)

    async def handle_general_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        self._discover(update)
        await _routing.handle_general_message(self, update, context)

    # -- Lifecycle --

    async def start(self) -> None:
        if not self.enabled:
            log.info("Telegram bot disabled (TELEGRAM_TOKEN not set)")
            return
        if not self.config.telegram.allowed_chat_ids:
            log.error(
                "Telegram bot NOT started: telegram.allowed_chat_ids is empty. "
                "Run `backbone config set telegram.allowed_chat_ids '[<your chat id>]'`."
            )
            return
        app = self.build_app()
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        self._running = True
        log.info("Telegram bot polling started")
        await self.sync_agents()

    async def stop(self) -> None:
        if self._app is None or not self._running:
            return
        try:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        finally:
            self._running = False
            self._app = None

    @staticmethod
    async def _on_handler_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """One warning line per failed handler instead of PTB's full traceback.

        A timed-out reply or a Telegram API hiccup must not read like a
        crash in the backbone's log; the update it belonged to is logged so
        it can be traced, and polling simply continues.
        """
        chat = getattr(getattr(update, "effective_chat", None), "id", None)
        text = getattr(getattr(update, "message", None), "text", None) or ""
        # Never the text itself — it is a person's message and may hold secrets.
        log.warning(
            "Telegram handler failed (chat=%s, text_length=%d): %s",
            chat,
            len(text),
            context.error,
        )

    def build_app(self) -> Application:
        """Build the Telegram bot application with all command handlers."""
        token = self.config.telegram_token
        if not token:
            raise ValueError("TELEGRAM_TOKEN environment variable not set")

        # Generous HTTP timeouts: startup answers pending commands while the
        # topic sync is talking to the same API, and Telegram is sometimes slow.
        self._app = (
            Application.builder()
            .token(token)
            .connect_timeout(20)
            .read_timeout(30)
            .write_timeout(30)
            .build()
        )
        self._app.add_error_handler(self._on_handler_error)
        self._app.add_handler(CommandHandler("help", self.cmd_help))
        self._app.add_handler(CommandHandler("status", self.cmd_status))
        self._app.add_handler(CommandHandler("queue", self.cmd_queue))
        self._app.add_handler(CommandHandler("start", self.cmd_start_agent))
        self._app.add_handler(CommandHandler("stop", self.cmd_stop_agent))
        self._app.add_handler(CommandHandler("tell", self.cmd_tell))
        self._app.add_handler(CommandHandler("digest", self.cmd_digest))
        self._app.add_handler(CommandHandler("identify", self.cmd_identify))
        self._app.add_handler(CommandHandler("viewplan", self.cmd_viewplan))
        self._app.add_handler(CommandHandler("approve", self.cmd_approve))
        self._app.add_handler(CallbackQueryHandler(self.on_callback))
        self._app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.IS_TOPIC_MESSAGE,
                self.handle_topic_message,
            )
        )
        # Plain text in the group's General topic (no thread): learn the
        # group, point at the per-agent topics — never guess an agent.
        self._app.add_handler(
            MessageHandler(
                filters.TEXT
                & ~filters.COMMAND
                & ~filters.IS_TOPIC_MESSAGE
                & filters.ChatType.GROUPS,
                self.handle_general_message,
            )
        )
        return self._app
