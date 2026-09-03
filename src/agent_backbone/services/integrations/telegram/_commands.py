"""Telegram command handlers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import ContextTypes

if TYPE_CHECKING:
    from agent_backbone.services.integrations.telegram.interface import TelegramService

from agent_backbone.services.agents import (
    plan_control,
    read_plan,
    read_state_file,
    start_agent,
    stop_agent,
)
from agent_backbone.services.integrations.telegram._routing import _delivery_reply
from agent_backbone.services.integrations.telegram._topic_discovery import (
    process_message_for_discovery,
)
from agent_backbone.services.routing import safe_deliver
from agent_backbone.services.terminal import list_sessions, session_exists

log = logging.getLogger(__name__)


def _ref(row: dict) -> str:
    """Short reference for a delivery row: ``repo#N`` or the kind."""
    if row.get("issue_number"):
        return f"{row.get('repo') or ''}#{row['issue_number']}"
    return row.get("kind") or "message"


def _authorized(bot: TelegramService, update: Update) -> bool:
    return bool(update.effective_chat) and bot._is_authorized(update.effective_chat.id)


async def cmd_help(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show available commands."""
    if not _authorized(bot, update):
        return

    text = (
        "*agent-backbone — Commands*\n\n"
        "/status — Show active agent sessions\n"
        "/queue — Show pending & recent deliveries\n"
        "/digest — Full system digest (sessions, agents, pending)\n"
        "/tell `<agent>` `<message>` — Send a message to an agent\n"
        "/start `<agent>` — Start a configured agent\n"
        "/stop `<agent>` — Stop an agent session\n"
        "/viewplan `<agent>` — View an agent's pending plan\n"
        "/approve `<agent>` — Approve an agent's plan (if enabled)\n"
        "/identify — Show this topic's thread ID for routing config\n"
        "/help — This message"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_status(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show status of all agent sessions."""
    if not _authorized(bot, update):
        return

    sessions = set(await list_sessions())
    lines = ["*Agents:*"]
    for spec in bot.config.agents:
        mark = "\U0001f7e2" if spec.name in sessions else "⚪"
        lines.append(f"  {mark} `{spec.name}` ({spec.runtime})")
    others = sorted(s for s in sessions if s not in bot.config.agents)
    if others:
        lines.append("\n*Other tmux sessions:*")
        lines.extend(f"  • `{s}`" for s in others)
    if len(lines) == 1:
        lines.append("  (none configured)")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_queue(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show pending deliveries and recent delivery history."""
    if not _authorized(bot, update):
        return
    if bot._db is None:
        await update.message.reply_text("Database not available.")
        return

    recent = await bot._db.deliveries.query(limit=10)
    failed = await bot._db.deliveries.failed(limit=10)

    lines = []
    if failed:
        lines.append(f"*Failed/Pending:* {len(failed)}")
        for d in failed[:5]:
            lines.append(f"  • {_ref(d)} → {d['target_entity']} ({d['outcome']})")

    if recent:
        lines.append(f"\n*Recent Deliveries:* {len(recent)}")
        for d in recent[:5]:
            lines.append(f"  • {_ref(d)} → {d['target_entity']} ({d['outcome']})")

    text = "\n".join(lines) if lines else "No delivery records."
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_start_agent(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Start a configured agent: /start <agent_name>"""
    if not _authorized(bot, update):
        return

    if not context.args:
        await update.message.reply_text("Usage: /start <agent_name>")
        return

    name = context.args[0]
    spec = bot.config.agents.get(name)
    if spec is None:
        await update.message.reply_text(f"Unknown agent `{name}`", parse_mode="Markdown")
        return

    result = await start_agent(spec, bot.config, db=bot._db, wait=False)
    status = "Started" if result.ok else "Failed to start"
    await update.message.reply_text(f"{status} `{name}`", parse_mode="Markdown")


async def cmd_stop_agent(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Stop an agent session: /stop <agent_name>"""
    if not _authorized(bot, update):
        return

    if not context.args:
        await update.message.reply_text("Usage: /stop <agent_name>")
        return

    name = context.args[0]
    if name == bot.config.backbone.session_name:
        await update.message.reply_text("Refusing to stop the backbone's own session.")
        return
    if bot.config.agents.get(name) is None:
        await update.message.reply_text(f"Unknown agent `{name}`", parse_mode="Markdown")
        return
    ok = await stop_agent(name)
    status = "Stopped" if ok else "Failed to stop"
    await update.message.reply_text(f"{status} `{name}`", parse_mode="Markdown")


async def cmd_tell(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Send a message to an agent: /tell <agent> <message>"""
    if not _authorized(bot, update):
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: /tell <agent> <message>")
        return

    agent = context.args[0]
    if bot.config.agents.get(agent) is None:
        await update.message.reply_text(f"Unknown agent `{agent}`", parse_mode="Markdown")
        return
    raw_message = " ".join(context.args[1:])
    sender = bot._sender_tag(update)
    message = f"[via:telegram from:{sender}] {raw_message}"
    result = await safe_deliver(
        agent, message, bot.config, db=bot._db, delivery_kind="direct_message"
    )

    await update.message.reply_text(_delivery_reply(agent, result), parse_mode="Markdown")


async def cmd_digest(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show system digest -- sessions, pending issues, recent deliveries."""
    if not _authorized(bot, update):
        return

    sessions = await list_sessions()
    failed: list = []
    states: list = []
    if bot._db is not None:
        failed = await bot._db.deliveries.failed(limit=50)
        states = await bot._db.states.all()

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


async def cmd_identify(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Report the current topic's thread_id for routing configuration."""
    if not _authorized(bot, update):
        return

    thread_id = getattr(update.message, "message_thread_id", None)
    if thread_id is None:
        await update.message.reply_text(
            f"Not in a topic. Chat ID: `{update.effective_chat.id}`", parse_mode="Markdown"
        )
        return

    process_message_for_discovery(
        update,
        bot.config,
        bot._discovery,
        bot.config.telegram_topic_discovery_path,
    )

    config_routes = bot.config.telegram.topic_routes
    merged = bot._effective_routes()
    mapping = merged.get(thread_id)

    if mapping:
        source = "config" if thread_id in config_routes else "auto-discovered"
        await update.message.reply_text(
            f"Thread ID: `{thread_id}`\nMapped to: `{mapping}` ({source})",
            parse_mode="Markdown",
        )
    else:
        topic_name = bot._discovery.topic_names.get(thread_id)
        name_line = f"\nTopic name: {topic_name}" if topic_name else ""
        await update.message.reply_text(
            f"Thread ID: `{thread_id}`\n"
            f"Not yet mapped.{name_line}\n"
            f"Map it with:\n"
            f"```\nbackbone config set telegram.topic_routes "
            f'\'{{"{thread_id}": "agent-name"}}\'\n```',
            parse_mode="Markdown",
        )


async def cmd_viewplan(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """View an agent's pending plan: /viewplan <agent>"""
    if not _authorized(bot, update):
        return

    if not context.args:
        await update.message.reply_text("Usage: /viewplan <agent>")
        return

    agent = context.args[0]
    snapshot = read_state_file(bot.config.state_dir, agent)

    if not snapshot or not snapshot.is_plan_waiting:
        state_str = snapshot.state.value if snapshot else "unknown"
        await update.message.reply_text(
            f"Agent `{agent}` is not waiting for plan approval (state: {state_str})",
            parse_mode="Markdown",
        )
        return

    plan_content = read_plan(bot.config.state_dir, snapshot)
    if plan_content is None:
        await update.message.reply_text(
            f"Agent `{agent}` has no readable plan file (plans live under the state directory)."
        )
        return

    header = f"Plan: {snapshot.plan_title or 'Untitled'}\nAgent: {agent}\n\n"
    full_text = header + plan_content

    max_len = 4096
    for i in range(0, len(full_text), max_len):
        await update.message.reply_text(full_text[i : i + max_len])


async def cmd_approve(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Approve an agent's plan with its runtime's own keys: /approve <agent>"""
    if not _authorized(bot, update):
        return

    if not bot.config.security.allow_remote_plan_control:
        await update.message.reply_text(
            "Remote plan approval is disabled. Enable with "
            "`backbone config set security.allow_remote_plan_control true`.",
            parse_mode="Markdown",
        )
        return

    if not context.args:
        await update.message.reply_text("Usage: /approve <agent>")
        return

    agent = context.args[0]
    if bot.config.agents.get(agent) is None:
        await update.message.reply_text(f"Unknown agent `{agent}`", parse_mode="Markdown")
        return
    snapshot = read_state_file(bot.config.state_dir, agent)

    if not snapshot or not snapshot.is_plan_waiting:
        state_str = snapshot.state.value if snapshot else "unknown"
        await update.message.reply_text(
            f"Agent `{agent}` is not waiting for plan approval (state: {state_str})",
            parse_mode="Markdown",
        )
        return

    if not await session_exists(agent):
        await update.message.reply_text(f"Session `{agent}` is offline.", parse_mode="Markdown")
        return

    spec = bot.config.agents.get(agent)
    outcome, evidence = await plan_control(agent, "approve", runtime=spec.runtime if spec else None)
    if outcome == "approved":
        await update.message.reply_text(f"Plan approved for `{agent}`.", parse_mode="Markdown")
    elif outcome == "unsupported":
        await update.message.reply_text(
            f"Plan approval is not available for `{agent}`: {evidence[0]}", parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"Could not approve the plan for `{agent}` ({outcome}): {evidence[0]}",
            parse_mode="Markdown",
        )
