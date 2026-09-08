"""Authorized, bounded report browsing and chat-bound navigation."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from agent_backbone.config import TelegramConfig
from agent_backbone.models import report_example
from agent_backbone.services.integrations.telegram import TelegramService, _updates
from tests.report_support import author, publication, publish


def bot_for(config, db):
    return TelegramService(
        replace(config, telegram=TelegramConfig(allowed_chat_ids=(111, 222))), db=db
    )


def message_update(chat=111):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )


def callback_update(data, chat=111):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat),
        callback_query=SimpleNamespace(
            data=data, answer=AsyncMock(), message=SimpleNamespace(reply_text=AsyncMock())
        ),
    )


async def test_authorized_reads_escaped_report_with_links_and_history(config, db):
    await author(db)
    data = report_example()
    data["progress"]["text"] = 'Payments explain <failed> & "retry" safely.'
    data["goal"]["links"][0] = {
        "title": "Checkout <feedback>",
        "url": "https://example.com/issue?a=1&b=2",
    }
    request = publication(key="escaped")
    request.report = request.report.model_validate(data)
    record, _ = await db.reports.publish(request)
    bot = bot_for(config, db)
    update = message_update()
    await bot.cmd_updates(update, SimpleNamespace(args=["show", str(record["id"])]))
    text = "\n".join(call.args[0] for call in update.message.reply_text.await_args_list)
    assert "&lt;failed&gt; &amp; &quot;retry&quot;" in text
    assert 'href="https://example.com/issue?a=1&amp;b=2"' in text
    assert "Checkout &lt;feedback&gt;" in text
    markup = update.message.reply_text.await_args.kwargs["reply_markup"]
    callback = callback_update(markup.inline_keyboard[0][0].callback_data)
    await bot.on_callback(callback, SimpleNamespace())
    assert "Report history" in callback.callback_query.message.reply_text.await_args.args[0]


async def test_latest_older_and_agent_filters_use_same_store(config, db):
    for name in ("a", "b", "c", "d", "missing"):
        await author(db, name)
        if name != "missing":
            await publish(db, name)
    bot = bot_for(config, db)
    update = message_update()
    await bot.cmd_updates(update, SimpleNamespace(args=[]))
    markup = update.message.reply_text.await_args.kwargs["reply_markup"]
    older = next(
        button for row in markup.inline_keyboard for button in row if button.text == "Older / more"
    )
    assert len(older.callback_data.encode()) <= 64
    callback = callback_update(older.callback_data)
    await bot.on_callback(callback, SimpleNamespace())
    text = "\n".join(
        call.args[0] for call in callback.callback_query.message.reply_text.await_args_list
    )
    assert "missing" in text and "no report yet" in text
    selected = message_update()
    await bot.cmd_updates(selected, SimpleNamespace(args=["a"]))
    text = selected.message.reply_text.await_args.args[0]
    assert "<b>a</b>" in text and "<b>b</b>" not in text
    history = message_update()
    await bot.cmd_updates(history, SimpleNamespace(args=["history", "a"]))
    assert "Report history" in history.message.reply_text.await_args.args[0]


async def test_unauthorized_commands_and_cross_chat_or_expired_buttons_cannot_read(config, db):
    bot = bot_for(config, db)
    update = message_update(chat=999)
    await bot.cmd_updates(update, SimpleNamespace(args=[]))
    update.message.reply_text.assert_not_awaited()
    button = _updates._button(bot, 111, "View", {"query": {"limit": 3}})
    for chat in (999, 222):
        callback = callback_update(button.callback_data, chat)
        await bot.on_callback(callback, SimpleNamespace())
        callback.callback_query.message.reply_text.assert_not_awaited()
        assert callback.callback_query.answer.await_args.args
    token = button.callback_data.split(":")[1]
    bot._report_views[token]["expires"] = 0
    callback = callback_update(button.callback_data)
    await bot.on_callback(callback, SimpleNamespace())
    assert "expired" in callback.callback_query.answer.await_args.args[0]
    callback.callback_query.message.reply_text.assert_not_awaited()


async def test_worst_case_unicode_markup_and_links_fit_telegram_messages(config, db):
    await author(db)
    request = publication()
    data = request.report.model_dump()
    for name, size in (("goal", 240), ("progress", 480), ("blockers", 200), ("next", 200)):
        data[name]["text"] = ("&😀" * size)[:size]
        data[name]["links"] = []
    link = {"title": "&" * 60, "url": "https://example.com/?" + "&" * 375}
    data["goal"]["links"] = [link, link]
    data["progress"]["links"] = [link, link]
    request.report = request.report.model_validate(data)
    record, _ = await db.reports.publish(request)
    bot = bot_for(config, db)
    update = message_update()
    await bot.cmd_updates(update, SimpleNamespace(args=["show", str(record["id"])]))
    assert update.message.reply_text.await_count > 1
    for call in update.message.reply_text.await_args_list:
        assert len(call.args[0].encode("utf-16-le")) // 2 <= 3500
        assert call.args[0].count("<a ") == call.args[0].count("</a>")
        assert call.kwargs["disable_web_page_preview"] is True


def test_navigation_cache_is_bounded_and_contains_only_views(config):
    bot = bot_for(config, MagicMock())
    for _ in range(300):
        _updates._button(bot, 111, "View", {"id": 1})
    assert len(bot._report_views) == 256
    assert all(value["view"] == {"id": 1} for value in bot._report_views.values())


async def test_missing_database_invalid_selection_and_missing_report(config, db):
    update = message_update()
    await bot_for(config, None).cmd_updates(update, SimpleNamespace(args=[]))
    assert update.message.reply_text.await_args.args[0] == "Database not available."
    bot = bot_for(config, db)
    for args in (["unknown"], ["show", "0"], ["bad\nname"]):
        await bot.cmd_updates(update, SimpleNamespace(args=args))
        assert "/updates" in update.message.reply_text.await_args.args[0]
    await bot.cmd_updates(update, SimpleNamespace(args=["show", "999"]))
    assert "not found" in update.message.reply_text.await_args.args[0]
