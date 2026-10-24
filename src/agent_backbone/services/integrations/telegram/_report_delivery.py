"""Independent, durable General/topic text and optional full-report voice deliveries."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from telegram import InlineKeyboardMarkup, ReplyParameters

from agent_backbone.services.integrations.telegram import _responses, _updates
from agent_backbone.services.integrations.telegram._speech import generate_voice
from agent_backbone.services.integrations.telegram._topic_discovery import agent_topic

if TYPE_CHECKING:
    from agent_backbone.services.integrations.telegram.interface import TelegramService

log = logging.getLogger(__name__)
_TIMEOUTS = {"read_timeout": 10, "write_timeout": 10, "connect_timeout": 10, "pool_timeout": 10}


def enabled_group(bot: TelegramService) -> int | None:
    group = bot._effective_group_chat_id()
    if bot._db is None or bot._app is None or not group or not bot._is_authorized(group):
        return None
    return group


async def flush_text(bot: TelegramService) -> None:
    group = enabled_group(bot)
    if group is None:
        return
    for _ in range(10):
        claim = await bot._db.reports.claim_telegram(
            include_reports=bot.config.telegram.report_updates
        )
        if claim is None:
            return
        record, parts = claim["record"], claim["parts"]
        report_id, lease = record["id"], claim["lease"]
        is_response = "response" in record
        if not parts:
            spec = bot.config.agents.get(record["agent_name"] or "")
            parts = {
                "group": group,
                "topic_required": is_response or bool(spec and spec.swarm is None),
                "audio": bot.config.telegram.report_audio,
                **({"response_joined_chunks": True} if is_response else {}),
            }
            if not await bot._db.reports.checkpoint_telegram(report_id, lease, parts):
                return
        complete = False
        try:
            if parts["group"] != group:
                raise ValueError("report destination group changed")
            if parts["topic_required"] and not parts.get("thread"):
                parts["thread"] = agent_topic(bot.config, bot._discovery, record["agent_name"])
            if is_response:
                if not parts.get("thread"):
                    raise ValueError("response topic is unavailable")
                chunks = _responses.response_chunks(
                    record, joined=parts.get("response_joined_chunks", False)
                )
                sent = parts.setdefault("response_messages", [])
                for text in chunks[len(sent) :]:
                    message = await bot._app.bot.send_message(
                        chat_id=group,
                        message_thread_id=parts["thread"],
                        text=text,
                        parse_mode=None,
                        disable_web_page_preview=True,
                        **_TIMEOUTS,
                    )
                    sent.append(message.message_id)
                    parts["topic"] = sent[0]
                    if not await bot._db.reports.checkpoint_telegram(report_id, lease, parts):
                        return
                complete = len(sent) == len(chunks)
            else:
                complete = await _send_report(bot, group, record, parts, report_id, lease)
                if complete is None:
                    return
        except Exception as exc:
            log.warning(
                "Report/response %s text delivery failed (%s); retry scheduled",
                report_id,
                type(exc).__name__,
            )
        destination = "topic" if is_response else "general"
        await bot._db.reports.finish_telegram(
            report_id,
            lease,
            message_id=f"{group}:{parts[destination]}" if complete else None,
            attempts=claim["attempts"],
            request_audio=parts["audio"],
        )


async def _send_report(bot, group, record, parts, report_id, lease) -> bool | None:
    """Keep the existing General/topic progress notification behavior."""
    buttons = InlineKeyboardMarkup(
        [
            [
                _updates._button(bot, group, "Full report", {"id": report_id}),
                _updates._button(bot, group, "Team updates", {"query": {"limit": 3}}),
            ]
        ]
    )
    for destination in ("general", "topic"):
        if parts.get(destination):
            continue
        if destination == "topic" and not parts.get("thread"):
            continue
        options = {"message_thread_id": parts["thread"]} if destination == "topic" else {}
        message = await bot._app.bot.send_message(
            chat_id=group,
            text=_updates.notification_text(record),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=buttons,
            **options,
            **_TIMEOUTS,
        )
        parts[destination] = message.message_id
        if not await bot._db.reports.checkpoint_telegram(report_id, lease, parts):
            return
    return bool(parts.get("general") and (not parts["topic_required"] or parts.get("topic")))


async def flush_audio(bot: TelegramService) -> None:
    group = enabled_group(bot)
    if group is None or not bot.config.telegram.report_audio:
        return
    # Audio is a separate scheduled lane; a slow or unavailable TTS cannot hold text.
    for _ in range(2):
        claim = await bot._db.reports.claim_telegram(
            audio=True, include_reports=bot.config.telegram.report_updates
        )
        if claim is None:
            return
        record, parts, targets = claim["record"], claim["parts"], claim["text_parts"]
        report_id, lease = record["id"], claim["lease"]
        complete = False
        try:
            if targets["group"] != group:
                raise ValueError("report destination group changed")
            voice = parts.get("file_id")
            if not voice:
                voice = await asyncio.wait_for(
                    generate_voice(record, bot.config.telegram), timeout=120
                )
            for destination in ("general", "topic"):
                if not targets.get(destination) or parts.get(destination):
                    continue
                options = {"message_thread_id": targets["thread"]} if destination == "topic" else {}
                message = await bot._app.bot.send_voice(
                    chat_id=group,
                    voice=voice,
                    filename=f"response-{report_id}.ogg"
                    if "response" in record
                    else f"report-{report_id}.ogg",
                    caption=(
                        f"{'Response' if 'response' in record else 'Full report'} {report_id} · "
                        f"{record['agent_name'] or record['author_name']}"
                    ),
                    reply_parameters=ReplyParameters(message_id=targets[destination]),
                    **options,
                    **_TIMEOUTS,
                )
                parts[destination] = message.message_id
                parts["file_id"] = message.voice.file_id
                voice = parts["file_id"]
                if not await bot._db.reports.checkpoint_telegram(
                    report_id, lease, parts, audio=True
                ):
                    return
            complete = True
        except Exception as exc:
            log.warning(
                "Report %s audio delivery failed (%s); text unaffected; retry scheduled",
                report_id,
                type(exc).__name__,
            )
        await bot._db.reports.finish_telegram(
            report_id,
            lease,
            message_id="sent" if complete else None,
            attempts=claim["attempts"],
            audio=True,
        )
