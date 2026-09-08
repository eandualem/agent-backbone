"""Read stored reports without contacting an agent or creating a Telegram topic."""

from __future__ import annotations

import html
import secrets
import time
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from agent_backbone.models import ProgressReport, ReportQuery

if TYPE_CHECKING:
    from agent_backbone.services.integrations.telegram.interface import TelegramService

_USAGE = (
    "Use /updates, /updates NAME, /updates history [NAME], or /updates show ID. "
    "Add --members to include swarm members."
)
_PAGE_SIZE = 3
_VIEW_TTL = 24 * 3600
_MAX_VIEWS = 256


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _age(record: dict) -> str:
    seconds = record["age_seconds"]
    age = (
        "just now"
        if seconds < 60
        else f"{seconds // 60}m ago"
        if seconds < 3600
        else f"{seconds // 3600}h ago"
        if seconds < 86400
        else f"{seconds // 86400}d ago"
    )
    return age + (" · old report" if record["stale"] else "")


def _button(bot: TelegramService, chat_id: int, title: str, view: dict):
    # Cursors can be longer than Telegram's 64-byte callback limit. A bounded,
    # chat-bound local token carries navigation only; an expired/restarted view
    # asks the person to reopen /updates. Nothing is delivered to an agent.
    now = time.monotonic()
    for key, stored in list(bot._report_views.items()):
        if stored["expires"] <= now:
            del bot._report_views[key]
    while len(bot._report_views) >= _MAX_VIEWS:
        bot._report_views.popitem(last=False)
    token = secrets.token_hex(8)
    bot._report_views[token] = {"chat": chat_id, "expires": now + _VIEW_TTL, "view": view}
    return InlineKeyboardButton(_clip(title, 48), callback_data=f"updates:{token}")


def _chunks(lines: list[str]) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = current + ("\n" if current else "") + line
        # Count UTF-16 units conservatively over escaped HTML too. Splits happen
        # between complete text/link lines, never inside an HTML tag or emoji.
        if len(candidate.encode("utf-16-le")) // 2 > 3500:
            if current:
                chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


async def _display(bot: TelegramService, message, chat_id: int, view: dict) -> None:
    if bot._db is None:
        await message.reply_text("Database not available.")
        return
    buttons = []
    if "id" in view:
        record = await bot._db.reports.get(view["id"])
        if not record:
            await message.reply_text("Report not found; it may have been pruned.")
            return
        report = ProgressReport.model_validate(record["report"])
        name = record["agent_name"] or f"{record['author_name']} (forgotten)"
        lines = [
            f"<b>{html.escape(name)}</b> · {report.status} · report {record['id']}",
            f"{_age(record)} · {record['created_at']}",
            f"Published as {html.escape(record['author_name'])}",
        ]
        for key, section in report.sections():
            label = key.capitalize()
            if key == "blockers":
                label += f" ({report.blockers.kind})"
            lines.append(f"\n<b>{label}</b>: {html.escape(section.text)}")
            lines.extend(
                f'<a href="{html.escape(link.url, quote=True)}">{html.escape(link.title)}</a>'
                for link in section.links
            )
        buttons.append(
            [
                _button(
                    bot,
                    chat_id,
                    "This author's history",
                    {
                        "query": ReportQuery(
                            history=True, author_id=record["author_id"], limit=_PAGE_SIZE
                        ).model_dump()
                    },
                )
            ]
        )
    else:
        query = ReportQuery.model_validate(view["query"])
        page = await bot._db.reports.query(query)
        lines = [
            "<b>Report history</b>" if query.history else "<b>Agent updates — latest reports</b>"
        ]
        for entry in page["items"]:
            record = entry["record"]
            if record is None:
                lines.append(f"\n<b>{html.escape(entry['agent_name'])}</b> · no report yet")
                continue
            report = record["report"]
            name = record["agent_name"] or f"{record['author_name']} (forgotten)"
            lines.append(f"\n<b>{html.escape(name)}</b> · {report['status']} · {_age(record)}")
            lines.append(f"Goal: {html.escape(_clip(report['goal']['text'], 100))}")
            lines.append(f"Progress: {html.escape(_clip(report['progress']['text'], 160))}")
            lines.append(
                f"Blockers ({report['blockers']['kind']}): "
                f"{html.escape(_clip(report['blockers']['text'], 100))}"
            )
            lines.append(f"Next: {html.escape(_clip(report['next']['text'], 100))}")
            buttons.append(
                [_button(bot, chat_id, f"{name} · report {record['id']}", {"id": record["id"]})]
            )
        if not page["items"]:
            lines.append("No retained reports." if query.history else "No agents in this view.")
        if page["next_cursor"]:
            older = query.model_dump() | {"cursor": page["next_cursor"]}
            buttons.append([_button(bot, chat_id, "Older / more", {"query": older})])
        if not query.history:
            history = query.model_dump() | {"history": True, "cursor": None}
            buttons.append([_button(bot, chat_id, "History", {"query": history})])
    buttons.append(
        [
            _button(
                bot,
                chat_id,
                "Latest from everyone",
                {"query": ReportQuery(limit=_PAGE_SIZE).model_dump()},
            )
        ]
    )
    chunks = _chunks(lines)
    for index, chunk in enumerate(chunks):
        await message.reply_text(
            chunk,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(buttons) if index == len(chunks) - 1 else None,
        )


async def cmd_updates(bot: TelegramService, update, context) -> None:
    if not update.effective_chat or not bot._is_authorized(update.effective_chat.id):
        return
    args = list(context.args or [])
    try:
        if args and args[0] == "show":
            if len(args) != 2 or not args[1].isdigit() or not 1 <= int(args[1]) <= 2**63 - 1:
                raise ValueError("Use /updates show ID with a positive report ID.")
            view = {"id": int(args[1])}
        else:
            history = bool(args and args[0] == "history")
            if history:
                args.pop(0)
            members = "--members" in args
            args = [arg for arg in args if arg != "--members"]
            view = {
                "query": ReportQuery(
                    agents=args, history=history, members=members, limit=_PAGE_SIZE
                ).model_dump()
            }
        await _display(bot, update.message, update.effective_chat.id, view)
    except ValueError:
        await update.message.reply_text(
            "That report selection is invalid or an agent is unknown. " + _USAGE
        )


async def on_callback(bot: TelegramService, update, context) -> None:
    query = update.callback_query
    if not update.effective_chat or not bot._is_authorized(update.effective_chat.id):
        await query.answer("Not allowed from this chat.")
        return
    token = (query.data or "").removeprefix("updates:")
    stored = bot._report_views.get(token)
    if (
        not stored
        or stored["chat"] != update.effective_chat.id
        or stored["expires"] <= time.monotonic()
        or query.message is None
    ):
        await query.answer("This view expired. Run /updates again.")
        return
    await query.answer()
    try:
        await _display(bot, query.message, update.effective_chat.id, stored["view"])
    except ValueError:
        await query.message.reply_text("This selection changed. Run /updates again.")
