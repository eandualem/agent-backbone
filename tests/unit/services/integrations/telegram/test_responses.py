"""Explicit answers use only the author topic and checkpoint every text chunk."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from agent_backbone.models import PublishResponse
from agent_backbone.services.integrations.telegram._responses import response_chunks, response_text
from agent_backbone.services.integrations.telegram._speech import spoken_report
from tests.report_support import author, publication
from tests.unit.api.routes.test_api_responses import body
from tests.unit.services.integrations.telegram.test_report_audio import configured_bot
from tests.unit.services.integrations.telegram.test_report_notifications import bot_for


async def test_partial_response_retry_and_audio_failure_do_not_duplicate_text(config, db):
    await author(db)
    request = body(agent="writer")
    request["response"]["answer"] = "👩‍💻" * 1900
    record, _ = await db.reports.publish(PublishResponse.model_validate(request))
    bot = configured_bot(config, db)
    # Explicit responses continue while progress notifications are paused.
    paused = replace(bot.config, telegram=replace(bot.config.telegram, report_updates=False))
    bot._config_provider = lambda: paused
    report, _ = await db.reports.publish(publication())
    chunks = response_chunks(record)
    assert chunks != response_chunks(record, joined=False)
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 4096 for chunk in chunks)
    assert response_text(record) == chunks[0] + "".join(
        chunk.split("\n", 1)[1] for chunk in chunks[1:]
    )
    bot._app.bot.send_message.side_effect = [
        SimpleNamespace(message_id=41),
        RuntimeError("offline"),
    ]
    await bot.flush_reports()
    assert (await db.reports.get(record["id"], kind="response"))["telegram_delivery"] == "pending"
    await db.agents.rename("writer", "renamed_writer_with_longer_name")
    async with db.engine.begin() as conn:
        await conn.execute(text("UPDATE reports SET telegram_retry_at = ''"))
    bot._app.bot.send_message.side_effect = [
        SimpleNamespace(message_id=42 + n) for n in range(len(chunks) - 1)
    ]
    await bot.flush_reports()
    sent = list(bot._app.bot.send_message.await_args_list)
    assert [call.kwargs["text"] for call in sent] == [chunks[0], chunks[1], *chunks[1:]]
    assert all(call.kwargs["message_thread_id"] == 77 for call in sent)
    assert all(call.kwargs["parse_mode"] is None for call in sent)
    assert (await db.reports.get(report["id"]))["telegram_delivery"] == "pending"
    assert (await db.reports.get(record["id"], kind="response"))["telegram_delivery"] == "sent"
    assert request["response"]["answer"] in spoken_report(record)

    with patch(
        "agent_backbone.services.integrations.telegram._report_delivery.generate_voice",
        AsyncMock(side_effect=RuntimeError("TTS unavailable")),
    ):
        await bot.flush_report_audio()
    saved = await db.reports.get(record["id"], kind="response")
    assert saved["telegram_delivery"] == "sent"
    assert saved["telegram_audio_delivery"] == "pending"
    await bot.flush_reports()
    assert len(sent) == bot._app.bot.send_message.await_count
    async with db.engine.begin() as conn:
        await conn.execute(text("UPDATE reports SET telegram_audio_retry_at = ''"))
    with patch(
        "agent_backbone.services.integrations.telegram._report_delivery.generate_voice",
        AsyncMock(return_value=b"OggSvoice"),
    ):
        await bot.flush_report_audio()
    bot._app.bot.send_voice.assert_awaited_once()
    assert bot._app.bot.send_voice.await_args.kwargs["message_thread_id"] == 77
    assert (await db.reports.get(record["id"], kind="response"))[
        "telegram_audio_delivery"
    ] == "sent"


async def test_response_waits_for_author_topic_without_general_fallback(config, db):
    await author(db)
    record, _ = await db.reports.publish(PublishResponse.model_validate(body(agent="writer")))
    bot = bot_for(config, db)
    await bot.flush_reports()
    bot._app.bot.send_message.assert_not_awaited()
    assert (await db.reports.get(record["id"], kind="response"))["telegram_delivery"] == "pending"


@pytest.mark.parametrize("sequence", ["👩‍💻", "🇪🇹", "👍🏽", "e\u0301"])
def test_response_chunks_keep_joined_characters(sequence):
    prefix = "a" * (3500 - len(sequence[0].encode("utf-16-le")) // 2)
    with patch(
        "agent_backbone.services.integrations.telegram._responses.response_text",
        return_value=prefix + sequence + "tail",
    ):
        chunks = response_chunks({"id": 1})
    assert chunks == [prefix, "Response 1 (continued)\n" + sequence + "tail"]


def test_oversized_combining_sequence_remains_bounded_and_complete():
    original = "a" + "\u0301" * 5000
    with patch(
        "agent_backbone.services.integrations.telegram._responses.response_text",
        return_value=original,
    ):
        chunks = response_chunks({"id": 1})
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 4096 for chunk in chunks)
    assert chunks[0] + "".join(chunk.split("\n", 1)[1] for chunk in chunks[1:]) == original


async def test_started_response_keeps_original_chunk_boundaries(config, db):
    await author(db)
    request = body(agent="writer")
    request["response"]["answer"] = "👩‍💻" * 1900
    record, _ = await db.reports.publish(PublishResponse.model_validate(request))
    old_chunks = response_chunks(record, joined=False)
    assert old_chunks != response_chunks(record)
    claim = await db.reports.claim_telegram()
    bot = configured_bot(config, db)
    # Before this change, the first message was sent and checkpointed without a format marker.
    parts = {
        "group": bot._effective_group_chat_id(),
        "thread": 77,
        "topic_required": True,
        "audio": False,
        "response_messages": [41],
        "topic": 41,
    }
    await db.reports.checkpoint_telegram(record["id"], claim["lease"], parts)
    await db.reports.finish_telegram(record["id"], claim["lease"], message_id=None, attempts=0)
    async with db.engine.begin() as conn:
        await conn.execute(text("UPDATE reports SET telegram_retry_at = ''"))
    bot._app.bot.send_message.side_effect = [
        SimpleNamespace(message_id=42 + n) for n in range(len(old_chunks) - 1)
    ]
    await bot.flush_reports()
    assert [call.kwargs["text"] for call in bot._app.bot.send_message.await_args_list] == (
        old_chunks[1:]
    )
    assert (await db.reports.get(record["id"], kind="response"))["telegram_delivery"] == "sent"
