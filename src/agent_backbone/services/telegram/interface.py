"""Telegram bot service — thin orchestration layer.

Commands are in _commands.py, topic routing in _routing.py,
topic discovery in _topic_discovery.py. This module wires them together.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from agent_backbone.config import BackboneConfig
from agent_backbone.services.telegram import _commands, _routing
from agent_backbone.services.telegram._topic_discovery import (
    effective_group_chat_id,
    effective_routes,
    load_discovery,
)
from agent_backbone.services.workflows import WorkflowRegistry

log = logging.getLogger(__name__)


# Module-level convenience alias for TelegramService.send_notification
async def send_notification(token: str, chat_id: int, text: str) -> bool:
    """Send a proactive push notification via Telegram API.

    Module-level convenience wrapper around TelegramService.send_notification.
    """
    return await TelegramService.send_notification(token, chat_id, text)


class TelegramService:
    """Telegram bot for agent backbone management."""

    def __init__(self, config: BackboneConfig) -> None:
        self._config = config
        self._app: Application | None = None
        self._registry = WorkflowRegistry()
        self._registry.discover()
        self._discovery = load_discovery(config.telegram.topic_discovery_path)

    def _effective_routes(self) -> dict[int, str]:
        """Merged routes: discovery + config (config wins)."""
        return effective_routes(self._config, self._discovery)

    def _effective_group_chat_id(self) -> int | None:
        """Group chat ID: config wins over discovery."""
        return effective_group_chat_id(self._config, self._discovery)

    @staticmethod
    def _sender_tag(update: Update) -> str:
        """Extract sender name from Telegram update for [via:telegram from:X] tag."""
        user = getattr(update, "effective_user", None)
        if user:
            return (user.first_name or user.username or "unknown").lower()
        return "unknown"

    def _is_authorized(self, chat_id: int) -> bool:
        """Check if a chat ID is authorized."""
        allowed = self._config.telegram.allowed_chat_ids
        return not allowed or chat_id in allowed

    async def _run_shell(self, cmd: str) -> tuple[str, int]:
        """Run a shell command and return (stdout+stderr, returncode)."""
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode().strip(), proc.returncode or 0

    @staticmethod
    async def send_notification(token: str, chat_id: int, text: str) -> bool:
        """Send a proactive push notification via Telegram API.

        Used by monitor flows where the bot isn't running. Direct httpx POST
        to the Telegram sendMessage endpoint.

        Returns True if the message was sent successfully.
        """
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text}
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=payload, timeout=10)
                if resp.status_code == 200:
                    return True
                log.warning(
                    "Telegram sendMessage failed: %s %s",
                    resp.status_code,
                    resp.text,
                )
                return False
        except httpx.HTTPError as e:
            log.warning("Telegram sendMessage error: %s", e)
            return False

    # -- Command handler thin wrappers (delegate to _commands module) --

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _commands.cmd_help(self, update, context)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    async def cmd_workflow(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _commands.cmd_workflow(self, update, context)

    async def cmd_identify(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _commands.cmd_identify(self, update, context)

    async def cmd_viewplan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _commands.cmd_viewplan(self, update, context)

    async def cmd_approve(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _commands.cmd_approve(self, update, context)

    async def handle_topic_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await _routing.handle_topic_message(self, update, context)

    # -- Lifecycle --

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def health_check(self) -> dict:
        return {"healthy": True, "service": "telegram"}

    def build_app(self) -> Application:
        """Build the Telegram bot application with all command handlers."""
        import os

        token = os.environ.get("TELEGRAM_TOKEN", "")
        if not token:
            raise ValueError("TELEGRAM_TOKEN environment variable not set")

        self._app = Application.builder().token(token).build()
        self._app.add_handler(CommandHandler("help", self.cmd_help))
        self._app.add_handler(CommandHandler("status", self.cmd_status))
        self._app.add_handler(CommandHandler("queue", self.cmd_queue))
        self._app.add_handler(CommandHandler("start", self.cmd_start_agent))
        self._app.add_handler(CommandHandler("stop", self.cmd_stop_agent))
        self._app.add_handler(CommandHandler("tell", self.cmd_tell))
        self._app.add_handler(CommandHandler("digest", self.cmd_digest))
        self._app.add_handler(CommandHandler("workflow", self.cmd_workflow))
        self._app.add_handler(CommandHandler("identify", self.cmd_identify))
        self._app.add_handler(CommandHandler("viewplan", self.cmd_viewplan))
        self._app.add_handler(CommandHandler("approve", self.cmd_approve))
        self._app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.IS_TOPIC_MESSAGE,
                self.handle_topic_message,
            )
        )
        return self._app

    async def run(self) -> None:
        """Run the bot with polling."""
        app = self.build_app()
        log.info("Starting Telegram bot...")
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        log.info("Bot is running. Press Ctrl+C to stop.")
        # Keep running until interrupted
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()


# Backward compatibility alias
BackboneBot = TelegramService
