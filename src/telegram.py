"""Telegram bot for agent backbone — commands and notification delivery.

Commands shell out to agent-services.sh for session management.
Authorization via config.telegram.allowed_chat_ids (empty = unrestricted).
"""

from __future__ import annotations

import asyncio
import logging
import shlex

import httpx
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.agent_state import AgentState, read_state_file
from src.config import BackboneConfig
from src.persistence import BackboneDB
from src.tmux import list_sessions, send_keys, session_exists
from src.topic_discovery import (
    effective_group_chat_id,
    effective_routes,
    load_discovery,
    process_message_for_discovery,
)
from src.workflow_registry import WorkflowRegistry
from telegram import Update

log = logging.getLogger(__name__)

SERVICES_SCRIPT = "~/.claude/services/agent-services.sh"


class BackboneBot:
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
                log.warning("Telegram sendMessage failed: %s %s", resp.status_code, resp.text)
                return False
        except httpx.HTTPError as e:
            log.warning("Telegram sendMessage error: %s", e)
            return False

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show available commands."""
        if not update.effective_chat or not self._is_authorized(update.effective_chat.id):
            return

        text = (
            "*Lovely Universe — Commands*\n\n"
            "/status — Show active agent sessions\n"
            "/queue — Show pending & recent deliveries\n"
            "/digest — Full system digest (sessions, agents, pending)\n"
            "/tell `<agent>` `<message>` — Send a message to an agent\n"
            "/start `<agent>` — Start an agent session\n"
            "/stop `<agent>` — Stop an agent session\n"
            "/workflow — List available workflows\n"
            "/workflow `<name>` — Run a workflow\n"
            "/viewplan `<agent>` — View an agent's pending plan\n"
            "/approve `<agent>` — Approve an agent's plan (sends Shift+Tab)\n"
            "/identify — Show this topic's thread ID for routing config\n"
            "/help — This message"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show status of all agent sessions."""
        if not update.effective_chat or not self._is_authorized(update.effective_chat.id):
            return

        sessions = await list_sessions()
        if not sessions:
            await update.message.reply_text("No active tmux sessions.")
            return

        lines = ["*Active Sessions:*"]
        for s in sorted(sessions):
            lines.append(f"  • `{s}`")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def cmd_queue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show pending deliveries and recent delivery history."""
        if not update.effective_chat or not self._is_authorized(update.effective_chat.id):
            return

        async with BackboneDB(str(self._config.delivery.db_file)) as db:
            recent = await db.query_deliveries(limit=10)
            failed = await db.get_failed_deliveries(limit=10)

        lines = []
        if failed:
            lines.append(f"*Failed/Pending:* {len(failed)}")
            for d in failed[:5]:
                lines.append(f"  • #{d['issue_number']} → {d['target_entity']} ({d['outcome']})")

        if recent:
            lines.append(f"\n*Recent Deliveries:* {len(recent)}")
            for d in recent[:5]:
                lines.append(f"  • #{d['issue_number']} → {d['target_entity']} ({d['outcome']})")

        text = "\n".join(lines) if lines else "No delivery records."
        await update.message.reply_text(text, parse_mode="Markdown")

    async def cmd_start_agent(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Start an agent session: /start <agent_name>"""
        if not update.effective_chat or not self._is_authorized(update.effective_chat.id):
            return

        if not context.args:
            await update.message.reply_text("Usage: /start <agent_name>")
            return

        agent = shlex.quote(context.args[0])
        output, rc = await self._run_shell(f"bash {SERVICES_SCRIPT} start {agent}")
        status = "Started" if rc == 0 else "Failed"
        await update.message.reply_text(
            f"{status} `{agent}`:\n```\n{output}\n```", parse_mode="Markdown"
        )

    async def cmd_stop_agent(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Stop an agent session: /stop <agent_name>"""
        if not update.effective_chat or not self._is_authorized(update.effective_chat.id):
            return

        if not context.args:
            await update.message.reply_text("Usage: /stop <agent_name>")
            return

        agent = shlex.quote(context.args[0])
        output, rc = await self._run_shell(f"bash {SERVICES_SCRIPT} stop {agent}")
        status = "Stopped" if rc == 0 else "Failed"
        await update.message.reply_text(
            f"{status} `{agent}`:\n```\n{output}\n```", parse_mode="Markdown"
        )

    async def cmd_tell(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send a message to an agent: /tell <agent> <message>"""
        if not update.effective_chat or not self._is_authorized(update.effective_chat.id):
            return

        if not context.args or len(context.args) < 2:
            await update.message.reply_text("Usage: /tell <agent> <message>")
            return

        from src.tmux import send_message

        agent = context.args[0]
        raw_message = " ".join(context.args[1:])
        sender = self._sender_tag(update)
        message = f"[via:telegram from:{sender}] {raw_message}"
        success = await send_message(agent, message)

        if success:
            await update.message.reply_text(f"Sent to `{agent}`.", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"Failed — `{agent}` offline?", parse_mode="Markdown")

    async def cmd_digest(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show system digest — sessions, pending issues, recent deliveries."""
        if not update.effective_chat or not self._is_authorized(update.effective_chat.id):
            return

        sessions = await list_sessions()
        async with BackboneDB(str(self._config.delivery.db_file)) as db:
            failed = await db.get_failed_deliveries(limit=50)
            states = await db.get_all_agent_states()

        lines = [
            "*System Digest*",
            f"Sessions: {len(sessions)} active",
            f"Pending deliveries: {len(failed)}",
            f"Tracked agents: {len(states)}",
        ]

        if states:
            lines.append("\n*Agent States:*")
            for s in states:
                issue_str = f" (#{s['current_issue']})" if s.get("current_issue") else ""
                lines.append(f"  • `{s['session_name']}`: {s['state']}{issue_str}")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def cmd_workflow(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """List or run a workflow: /workflow [name]"""
        if not update.effective_chat or not self._is_authorized(update.effective_chat.id):
            return

        if not context.args:
            text = self._registry.format_list()
            await update.message.reply_text(f"```\n{text}\n```", parse_mode="Markdown")
            return

        name = context.args[0]
        entry = self._registry.get(name)
        if not entry:
            names = ", ".join(self._registry.list_names())
            await update.message.reply_text(
                f"Unknown workflow `{name}`. Available: {names}", parse_mode="Markdown"
            )
            return

        await update.message.reply_text(f"Running workflow `{name}`...", parse_mode="Markdown")
        try:
            result = await entry.flow_fn()
            summary = ", ".join(f"{k}: {v}" for k, v in result.items()) if result else "done"
            await update.message.reply_text(
                f"Workflow `{name}` complete:\n```\n{summary}\n```", parse_mode="Markdown"
            )
        except Exception as exc:
            log.exception("Workflow '%s' failed", name)
            await update.message.reply_text(
                f"Workflow `{name}` failed: {exc}", parse_mode="Markdown"
            )

    async def cmd_identify(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Report the current topic's thread_id for routing configuration."""
        if not update.effective_chat or not self._is_authorized(update.effective_chat.id):
            return

        thread_id = getattr(update.message, "message_thread_id", None)
        if thread_id is None:
            await update.message.reply_text("Not in a topic.")
            return

        # Run discovery on this message
        process_message_for_discovery(
            update,
            self._config,
            self._discovery,
            self._config.telegram.topic_discovery_path,
        )

        config_routes = self._config.telegram.topic_routes
        merged = self._effective_routes()
        mapping = merged.get(thread_id)

        if mapping:
            if thread_id in config_routes:
                source = "config"
            else:
                source = "auto-discovered"
            await update.message.reply_text(
                f"Thread ID: `{thread_id}`\nMapped to: `{mapping}` ({source})",
                parse_mode="Markdown",
            )
        else:
            topic_name = self._discovery.topic_names.get(thread_id)
            name_line = f"\nTopic name: {topic_name}" if topic_name else ""
            await update.message.reply_text(
                f"Thread ID: `{thread_id}`\n"
                f"Not yet mapped.{name_line}\n"
                f"Add to `backbone.toml`:\n"
                f'```\n[telegram.topic_routes]\n{thread_id} = "session-name"\n```',
                parse_mode="Markdown",
            )

    async def cmd_viewplan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """View an agent's pending plan: /viewplan <agent>"""
        if not update.effective_chat or not self._is_authorized(update.effective_chat.id):
            return

        if not context.args:
            await update.message.reply_text("Usage: /viewplan <agent>")
            return

        agent = context.args[0]
        state_path = self._config.agent_state.state_path
        snapshot = read_state_file(state_path, agent)

        if not snapshot or snapshot.state != AgentState.PLAN_WAITING:
            state_str = snapshot.state.value if snapshot else "unknown"
            await update.message.reply_text(
                f"Agent `{agent}` is not waiting for plan approval (state: {state_str})",
                parse_mode="Markdown",
            )
            return

        if not snapshot.plan_file:
            await update.message.reply_text(f"Agent `{agent}` has no plan file path in state.")
            return

        try:
            from pathlib import Path

            plan_content = Path(snapshot.plan_file).read_text()
        except (OSError, FileNotFoundError) as e:
            await update.message.reply_text(f"Cannot read plan file: {e}")
            return

        header = f"Plan: {snapshot.plan_title or 'Untitled'}\nAgent: {agent}\n\n"
        full_text = header + plan_content

        # Telegram message limit is 4096 chars — split if needed
        max_len = 4096
        for i in range(0, len(full_text), max_len):
            chunk = full_text[i : i + max_len]
            await update.message.reply_text(chunk)

    async def cmd_approve(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Approve an agent's plan by sending Shift+Tab: /approve <agent>"""
        if not update.effective_chat or not self._is_authorized(update.effective_chat.id):
            return

        if not context.args:
            await update.message.reply_text("Usage: /approve <agent>")
            return

        agent = context.args[0]
        state_path = self._config.agent_state.state_path
        snapshot = read_state_file(state_path, agent)

        if not snapshot or snapshot.state != AgentState.PLAN_WAITING:
            state_str = snapshot.state.value if snapshot else "unknown"
            await update.message.reply_text(
                f"Agent `{agent}` is not waiting for plan approval (state: {state_str})",
                parse_mode="Markdown",
            )
            return

        if not await session_exists(agent):
            await update.message.reply_text(f"Session `{agent}` is offline.", parse_mode="Markdown")
            return

        # Send Shift+Tab: Escape then [Z (forms \e[Z)
        ok1 = await send_keys(agent, "Escape")
        ok2 = await send_keys(agent, "[Z")

        if ok1 and ok2:
            await update.message.reply_text(
                f"Plan approved for `{agent}`. Sending approval signal.",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                f"Failed to send approval keys to `{agent}`.",
                parse_mode="Markdown",
            )

    async def handle_topic_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Route plain text messages in forum topics to mapped tmux sessions."""
        if not update.effective_chat or not self._is_authorized(update.effective_chat.id):
            return

        thread_id = getattr(update.message, "message_thread_id", None)
        if thread_id is None:
            return

        # Run discovery before route lookup
        process_message_for_discovery(
            update,
            self._config,
            self._discovery,
            self._config.telegram.topic_discovery_path,
        )

        routes = self._effective_routes()
        target = routes.get(thread_id)
        if target is None:
            return

        text = (update.message.text or "").strip()
        if not text:
            return

        from src.tmux import send_message

        sender = self._sender_tag(update)
        tag = f"[via:telegram from:{sender}]"

        if target == "coding-agents":
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

        success = await send_message(agent, message)
        if success:
            await update.message.reply_text(f"Sent to `{agent}`.", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"Failed — `{agent}` offline?", parse_mode="Markdown")

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
