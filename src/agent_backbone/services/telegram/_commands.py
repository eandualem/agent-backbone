"""Telegram command handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import ContextTypes

if TYPE_CHECKING:
    from agent_backbone.services.telegram.interface import TelegramService

from agent_backbone.services.agents.interface import StateService
from agent_backbone.services.agents.models import AgentState, StateSnapshot
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.messaging import deliver_message
from agent_backbone.services.telegram._routing import _delivery_reply
from agent_backbone.services.telegram._topic_discovery import process_message_for_discovery
from agent_backbone.services.terminal import (
    RUNTIME_ENV_KEY,
    list_sessions,
    resolve_agent_dir,
    send_keys,
    session_exists,
    start_session,
    stop_session,
)


async def _reported_state(bot: TelegramService, agent: str) -> StateSnapshot:
    """Load one agent state snapshot from PostgreSQL for Telegram commands."""
    async with BackboneDB.connect(bot._config.database.async_url) as db:
        return await StateService(db=db).get_reported_state(agent)


async def cmd_help(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show available commands."""
    if not update.effective_chat or not bot._is_authorized(update.effective_chat.id):
        return

    text = (
        "*Lovely Universe \u2014 Commands*\n\n"
        "/status \u2014 Show active agent sessions\n"
        "/queue \u2014 Show pending & recent deliveries\n"
        "/digest \u2014 Full system digest (sessions, agents, pending)\n"
        "/tell `<agent>` `<message>` \u2014 Send a message to an agent\n"
        "/start `<agent>` \u2014 Start an agent session\n"
        "/stop `<agent>` \u2014 Stop an agent session\n"
        "/workflow \u2014 List available workflows\n"
        "/workflow `<name>` \u2014 Run a workflow\n"
        "/viewplan `<agent>` \u2014 View an agent's pending plan\n"
        "/approve `<agent>` \u2014 Approve an agent's plan (sends Shift+Tab)\n"
        "/identify \u2014 Show this topic's thread ID for routing config\n"
        "/help \u2014 This message"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_status(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show status of all agent sessions."""
    if not update.effective_chat or not bot._is_authorized(update.effective_chat.id):
        return

    sessions = await list_sessions()
    if not sessions:
        await update.message.reply_text("No active tmux sessions.")
        return

    lines = ["*Active Sessions:*"]
    for s in sorted(sessions):
        lines.append(f"  \u2022 `{s}`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_queue(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show pending deliveries and recent delivery history."""
    if not update.effective_chat or not bot._is_authorized(update.effective_chat.id):
        return

    async with BackboneDB.connect(bot._config.database.async_url) as db:
        recent = await db.query_deliveries(limit=10)
        failed = await db.get_failed_deliveries(limit=10)

    lines = []
    if failed:
        lines.append(f"*Failed/Pending:* {len(failed)}")
        for d in failed[:5]:
            lines.append(
                f"  \u2022 #{d['issue_number']} \u2192 {d['target_entity']} ({d['outcome']})"
            )

    if recent:
        lines.append(f"\n*Recent Deliveries:* {len(recent)}")
        for d in recent[:5]:
            lines.append(
                f"  \u2022 #{d['issue_number']} \u2192 {d['target_entity']} ({d['outcome']})"
            )

    text = "\n".join(lines) if lines else "No delivery records."
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_start_agent(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Start an agent session: /start <agent_name>"""
    if not update.effective_chat or not bot._is_authorized(update.effective_chat.id):
        return

    if not context.args:
        await update.message.reply_text("Usage: /start <agent_name>")
        return

    agent = context.args[0]
    working_dir = resolve_agent_dir(agent, bot._config.registry)
    if not working_dir:
        await update.message.reply_text(f"Unknown agent `{agent}`", parse_mode="Markdown")
        return

    ok = await start_session(
        agent,
        working_dir=working_dir,
        command=["claude"],
        environment={RUNTIME_ENV_KEY: "claude"},
    )
    status = "Started" if ok else "Failed to start"
    await update.message.reply_text(f"{status} `{agent}`", parse_mode="Markdown")


async def cmd_stop_agent(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Stop an agent session: /stop <agent_name>"""
    if not update.effective_chat or not bot._is_authorized(update.effective_chat.id):
        return

    if not context.args:
        await update.message.reply_text("Usage: /stop <agent_name>")
        return

    agent = context.args[0]
    ok = await stop_session(agent)
    status = "Stopped" if ok else "Failed to stop"
    await update.message.reply_text(f"{status} `{agent}`", parse_mode="Markdown")


async def cmd_tell(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Send a message to an agent: /tell <agent> <message>"""
    if not update.effective_chat or not bot._is_authorized(update.effective_chat.id):
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: /tell <agent> <message>")
        return

    agent = context.args[0]
    raw_message = " ".join(context.args[1:])
    sender = bot._sender_tag(update)
    message = f"[via:telegram from:{sender}] {raw_message}"
    result = await deliver_message(agent, message, bot._config)

    await update.message.reply_text(_delivery_reply(agent, result), parse_mode="Markdown")


async def cmd_digest(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show system digest -- sessions, pending issues, recent deliveries."""
    if not update.effective_chat or not bot._is_authorized(update.effective_chat.id):
        return

    sessions = await list_sessions()
    async with BackboneDB.connect(bot._config.database.async_url) as db:
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
            lines.append(f"  \u2022 `{s['session_name']}`: {s['state']}{issue_str}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_workflow(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """List or run a workflow: /workflow [name]"""
    if not update.effective_chat or not bot._is_authorized(update.effective_chat.id):
        return

    if not context.args:
        text = bot._registry.format_list()
        await update.message.reply_text(f"```\n{text}\n```", parse_mode="Markdown")
        return

    name = context.args[0]
    entry = bot._registry.get(name)
    if not entry:
        names = ", ".join(bot._registry.list_names())
        await update.message.reply_text(
            f"Unknown workflow `{name}`. Available: {names}",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(f"Running workflow `{name}`...", parse_mode="Markdown")
    try:
        result = await entry.flow_fn()
        summary = ", ".join(f"{k}: {v}" for k, v in result.items()) if result else "done"
        await update.message.reply_text(
            f"Workflow `{name}` complete:\n```\n{summary}\n```",
            parse_mode="Markdown",
        )
    except Exception as exc:
        import logging

        log = logging.getLogger(__name__)
        log.exception("Workflow '%s' failed", name)
        await update.message.reply_text(f"Workflow `{name}` failed: {exc}", parse_mode="Markdown")


async def cmd_identify(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Report the current topic's thread_id for routing configuration."""
    if not update.effective_chat or not bot._is_authorized(update.effective_chat.id):
        return

    thread_id = getattr(update.message, "message_thread_id", None)
    if thread_id is None:
        await update.message.reply_text("Not in a topic.")
        return

    # Run discovery on this message
    process_message_for_discovery(
        update,
        bot._config,
        bot._discovery,
        bot._config.telegram.topic_discovery_path,
    )

    config_routes = bot._config.telegram.topic_routes
    merged = bot._effective_routes()
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
        topic_name = bot._discovery.topic_names.get(thread_id)
        name_line = f"\nTopic name: {topic_name}" if topic_name else ""
        await update.message.reply_text(
            f"Thread ID: `{thread_id}`\n"
            f"Not yet mapped.{name_line}\n"
            f"Add to `backbone.toml`:\n"
            f'```\n[telegram.topic_routes]\n{thread_id} = "session-name"\n```',
            parse_mode="Markdown",
        )


async def cmd_viewplan(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """View an agent's pending plan: /viewplan <agent>"""
    if not update.effective_chat or not bot._is_authorized(update.effective_chat.id):
        return

    if not context.args:
        await update.message.reply_text("Usage: /viewplan <agent>")
        return

    agent = context.args[0]
    snapshot = await _reported_state(bot, agent)

    if not snapshot or snapshot.state != AgentState.PLAN_WAITING:
        state_str = snapshot.state.value if snapshot else AgentState.OFFLINE.value
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

    # Telegram message limit is 4096 chars -- split if needed
    max_len = 4096
    for i in range(0, len(full_text), max_len):
        chunk = full_text[i : i + max_len]
        await update.message.reply_text(chunk)


async def cmd_approve(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Approve an agent's plan by sending Shift+Tab: /approve <agent>"""
    if not update.effective_chat or not bot._is_authorized(update.effective_chat.id):
        return

    if not context.args:
        await update.message.reply_text("Usage: /approve <agent>")
        return

    agent = context.args[0]
    snapshot = await _reported_state(bot, agent)

    if not snapshot or snapshot.state != AgentState.PLAN_WAITING:
        state_str = snapshot.state.value if snapshot else AgentState.OFFLINE.value
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
