"""Report pushes use the allowed shared group and persist their delivery outcome."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from agent_backbone.config import TelegramConfig
from agent_backbone.services.integrations.telegram import TelegramService
from tests.report_support import author, publication


def bot_for(config, db, **settings):
    config = replace(
        config,
        telegram=TelegramConfig(
            allowed_chat_ids=(-123,),
            group_chat_id=-123,
            **settings,
        ),
    )
    bot = TelegramService(config, db=db)
    bot._app = SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
    )
    return bot


async def test_push_to_shared_group_with_report_and_navigation(config, db):
    await author(db)
    request = publication()
    request.report.progress.text = "A readable <update> & working links."
    record, _ = await db.reports.publish(request)
    bot = bot_for(config, db)
    await bot.flush_reports()
    call = bot._app.bot.send_message.await_args.kwargs
    assert call["chat_id"] == -123 and "message_thread_id" not in call
    assert "&lt;update&gt; &amp;" in call["text"] and "<b>Goal</b>" in call["text"]
    assert "<b>Next</b>" in call["text"] and "<a href=" in call["text"]
    assert len(call["reply_markup"].inline_keyboard[0]) == 2
    assert (await db.reports.get(record["id"]))["telegram_delivery"] == "sent"
    await bot.flush_reports()
    bot._app.bot.send_message.assert_awaited_once()


@pytest.mark.parametrize("condition", ["paused", "no_group", "unallowed", "not_started"])
async def test_no_push_without_enabled_allowed_group(config, db, condition):
    await author(db)
    record, _ = await db.reports.publish(publication())
    bot = bot_for(config, db, report_updates=condition != "paused")
    if condition == "no_group":
        bot._effective_group_chat_id = lambda: None
    elif condition == "unallowed":
        bot._effective_group_chat_id = lambda: -999
    elif condition == "not_started":
        bot._app = None
    await bot.flush_reports()
    assert (await db.reports.get(record["id"]))["telegram_delivery"] == "pending"
    if bot._app:
        bot._app.bot.send_message.assert_not_awaited()


async def test_telegram_failure_is_retried_and_does_not_lose_report(config, db):
    await author(db)
    record, _ = await db.reports.publish(publication())
    bot = bot_for(config, db)
    bot._app.bot.send_message.side_effect = RuntimeError("offline")
    await bot.flush_reports()
    assert (await db.reports.get(record["id"]))["telegram_delivery"] == "pending"
    async with db._engine.begin() as conn:
        await conn.execute(text("UPDATE reports SET telegram_retry_at = ''"))
    bot._app.bot.send_message.side_effect = None
    await bot.flush_reports()
    assert (await db.reports.get(record["id"]))["telegram_delivery"] == "sent"


async def test_maximum_unicode_report_fits_one_telegram_message(config, db):
    from html.parser import HTMLParser

    from agent_backbone.services.integrations.telegram._updates import notification_text

    class VisibleText(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []

        def handle_data(self, data):
            self.parts.append(data)

    await author(db)
    request = publication()
    for section, length in [("goal", 240), ("progress", 480), ("blockers", 320), ("next", 320)]:
        getattr(request.report, section).text = "🧪" * length
        getattr(request.report, section).links = []
    record, _ = await db.reports.publish(request)
    parser = VisibleText()
    parser.feed(notification_text(record))
    assert len("".join(parser.parts).encode("utf-16-le")) // 2 <= 4096


@pytest.mark.parametrize("role", ["worker", "coordinator", "forgotten"])
async def test_old_swarm_text_and_audio_jobs_are_retired(config, db, role):
    # Simulate a report published by the previous version, including a partly
    # delivered text report with pending audio and an abandoned lease.
    await author(db)
    record, _ = await db.reports.publish(publication())
    # An ordinary agent with no report identity must not turn exclusion into SQL NULL.
    await author(db, name="ordinary_without_report")
    if role == "forgotten":
        async with db.engine.begin() as conn:
            await conn.execute(text("DELETE FROM agents WHERE name = 'writer'"))
    else:
        await author(db, tags=("swarm:demo", f"role:{role}"))
    async with db.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE reports SET telegram_delivery='sending', telegram_lease='old', "
                "telegram_retry_at='2099-01-01', telegram_audio_delivery='pending'"
            )
        )
    bot = bot_for(config, db, report_audio=True)
    bot._app.bot.send_voice = AsyncMock()
    await bot.flush_reports()
    await bot.flush_report_audio()
    bot._app.bot.send_message.assert_not_awaited()
    bot._app.bot.send_voice.assert_not_awaited()
    saved = await db.reports.get(record["id"])
    assert saved["telegram_delivery"] == saved["telegram_audio_delivery"] == "not_requested"
    assert saved["report"] == publication().report.model_dump()
