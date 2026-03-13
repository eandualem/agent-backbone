"""Telegram command handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import ContextTypes

if TYPE_CHECKING:
    from agent_backbone.services.telegram.interface import TelegramService

from agent_backbone.services.agents._file_reader import read_state_file
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.routing import safe_deliver
from agent_backbone.services.telegram._helpers import (
    authorized_message,
    read_plan_waiting_snapshot,
    split_message_chunks,
)
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


async def cmd_help(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show available commands."""
    message = authorized_message(bot, update)
    if message is None:
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
    await message.reply_text(text, parse_mode="Markdown")


async def cmd_status(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show status of all agent sessions."""
    message = authorized_message(bot, update)
    if message is None:
        return

    sessions = await list_sessions()
    if not sessions:
        await message.reply_text("No active tmux sessions.")
        return

    lines = ["*Active Sessions:*"]
    for s in sorted(sessions):
        lines.append(f"  \u2022 `{s}`")
    await message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_queue(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show pending deliveries and recent delivery history."""
    message = authorized_message(bot, update)
    if message is None:
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
    await message.reply_text(text, parse_mode="Markdown")


async def cmd_start_agent(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Start an agent session: /start <agent_name>"""
    message = authorized_message(bot, update)
    if message is None:
        return

    if not context.args:
        await message.reply_text("Usage: /start <agent_name>")
        return

    agent = context.args[0]
    working_dir = resolve_agent_dir(agent, bot._config.registry)
    if not working_dir:
        await message.reply_text(f"Unknown agent `{agent}`", parse_mode="Markdown")
        return

    ok = await start_session(
        agent,
        working_dir=working_dir,
        command=["claude"],
        environment={RUNTIME_ENV_KEY: "claude"},
    )
    status = "Started" if ok else "Failed to start"
    await message.reply_text(f"{status} `{agent}`", parse_mode="Markdown")


async def cmd_stop_agent(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Stop an agent session: /stop <agent_name>"""
    message = authorized_message(bot, update)
    if message is None:
        return

    if not context.args:
        await message.reply_text("Usage: /stop <agent_name>")
        return

    agent = context.args[0]
    ok = await stop_session(agent)
    status = "Stopped" if ok else "Failed to stop"
    await message.reply_text(f"{status} `{agent}`", parse_mode="Markdown")


async def cmd_tell(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Send a message to an agent: /tell <agent> <message>"""
    message = authorized_message(bot, update)
    if message is None:
        return

    if not context.args or len(context.args) < 2:
        await message.reply_text("Usage: /tell <agent> <message>")
        return

    agent = context.args[0]
    raw_message = " ".join(context.args[1:])
    sender = bot._sender_tag(update)
    tagged_message = f"[via:telegram from:{sender}] {raw_message}"
    result = await safe_deliver(agent, tagged_message, bot._config)

    await message.reply_text(_delivery_reply(agent, result), parse_mode="Markdown")


async def cmd_digest(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show system digest -- sessions, pending issues, recent deliveries."""
    message = authorized_message(bot, update)
    if message is None:
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

    await message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_workflow(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """List or run a workflow: /workflow [name]"""
    message = authorized_message(bot, update)
    if message is None:
        return

    if not context.args:
        text = bot._registry.format_list()
        await message.reply_text(f"```\n{text}\n```", parse_mode="Markdown")
        return

    name = context.args[0]
    entry = bot._registry.get(name)
    if not entry:
        names = ", ".join(bot._registry.list_names())
        await message.reply_text(
            f"Unknown workflow `{name}`. Available: {names}",
            parse_mode="Markdown",
        )
        return

    await message.reply_text(f"Running workflow `{name}`...", parse_mode="Markdown")
    try:
        result = await entry.flow_fn()
        summary = ", ".join(f"{k}: {v}" for k, v in result.items()) if result else "done"
        await message.reply_text(
            f"Workflow `{name}` complete:\n```\n{summary}\n```",
            parse_mode="Markdown",
        )
    except Exception as exc:
        import logging

        log = logging.getLogger(__name__)
        log.exception("Workflow '%s' failed", name)
        await message.reply_text(f"Workflow `{name}` failed: {exc}", parse_mode="Markdown")


async def cmd_identify(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Report the current topic's thread_id for routing configuration."""
    message = authorized_message(bot, update)
    if message is None:
        return

    thread_id = getattr(message, "message_thread_id", None)
    if thread_id is None:
        await message.reply_text("Not in a topic.")
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
        await message.reply_text(
            f"Thread ID: `{thread_id}`\nMapped to: `{mapping}` ({source})",
            parse_mode="Markdown",
        )
    else:
        topic_name = bot._discovery.topic_names.get(thread_id)
        name_line = f"\nTopic name: {topic_name}" if topic_name else ""
        await message.reply_text(
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
    message = authorized_message(bot, update)
    if message is None:
        return

    if not context.args:
        await message.reply_text("Usage: /viewplan <agent>")
        return

    agent = context.args[0]
    snapshot = read_plan_waiting_snapshot(bot, agent)

    if snapshot is None:
        state_snapshot = read_state_file(bot._config.agent_state.state_path, agent)
        state_str = state_snapshot.state.value if state_snapshot else "unknown"
        await message.reply_text(
            f"Agent `{agent}` is not waiting for plan approval (state: {state_str})",
            parse_mode="Markdown",
        )
        return

    if not snapshot.plan_file:
        await message.reply_text(f"Agent `{agent}` has no plan file path in state.")
        return

    try:
        from pathlib import Path

        plan_content = Path(snapshot.plan_file).read_text()
    except (OSError, FileNotFoundError) as e:
        await message.reply_text(f"Cannot read plan file: {e}")
        return

    header = f"Plan: {snapshot.plan_title or 'Untitled'}\nAgent: {agent}\n\n"
    full_text = header + plan_content

    for chunk in split_message_chunks(full_text):
        await message.reply_text(chunk)


async def cmd_approve(
    bot: TelegramService, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Approve an agent's plan by sending Shift+Tab: /approve <agent>"""
    message = authorized_message(bot, update)
    if message is None:
        return

    if not context.args:
        await message.reply_text("Usage: /approve <agent>")
        return

    agent = context.args[0]
    snapshot = read_plan_waiting_snapshot(bot, agent)

    if snapshot is None:
        state_snapshot = read_state_file(bot._config.agent_state.state_path, agent)
        state_str = state_snapshot.state.value if state_snapshot else "unknown"
        await message.reply_text(
            f"Agent `{agent}` is not waiting for plan approval (state: {state_str})",
            parse_mode="Markdown",
        )
        return

    if not await session_exists(agent):
        await message.reply_text(f"Session `{agent}` is offline.", parse_mode="Markdown")
        return

    # Send Shift+Tab: Escape then [Z (forms \e[Z)
    ok1 = await send_keys(agent, "Escape")
    ok2 = await send_keys(agent, "[Z")

    if ok1 and ok2:
        await message.reply_text(
            f"Plan approved for `{agent}`. Sending approval signal.",
            parse_mode="Markdown",
        )
    else:
        await message.reply_text(
            f"Failed to send approval keys to `{agent}`.",
            parse_mode="Markdown",
        )
