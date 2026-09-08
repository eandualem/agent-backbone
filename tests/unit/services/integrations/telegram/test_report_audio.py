"""Two destinations, independent text/audio retries, and optional local speech."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from agent_backbone.config import AgentsConfig, AgentSpec, validate_setting
from agent_backbone.services.integrations.telegram._speech import spoken_report
from tests.report_support import author, publication
from tests.unit.services.integrations.telegram.test_report_notifications import bot_for


def configured_bot(config, db):
    config = replace(config, agents=AgentsConfig({"writer": AgentSpec(name="writer", dir="/tmp")}))
    bot = bot_for(config, db, report_audio=True, topic_routes={77: "writer"})
    bot._app.bot.send_voice = AsyncMock(
        return_value=SimpleNamespace(
            message_id=52,
            voice=SimpleNamespace(file_id="telegram-voice-file"),
        )
    )
    return bot


async def test_partial_topic_retry_does_not_duplicate_general_and_queues_audio(db, config):
    await author(db)
    record, _ = await db.reports.publish(publication())
    bot = configured_bot(config, db)
    bot._app.bot.send_message.side_effect = [
        SimpleNamespace(message_id=41),
        RuntimeError("offline"),
    ]
    await bot.flush_reports()
    assert (await db.reports.get(record["id"]))["telegram_delivery"] == "pending"
    assert (await db.reports.get(record["id"]))["telegram_audio_delivery"] == "not_requested"
    async with db._engine.begin() as conn:
        await conn.execute(text("UPDATE reports SET telegram_retry_at = ''"))
    bot._app.bot.send_message.side_effect = [SimpleNamespace(message_id=42)]
    await bot.flush_reports()
    calls = bot._app.bot.send_message.await_args_list
    assert "message_thread_id" not in calls[0].kwargs
    assert [c.kwargs.get("message_thread_id") for c in calls[1:]] == [77, 77]
    assert (await db.reports.get(record["id"]))["telegram_delivery"] == "sent"
    assert (await db.reports.get(record["id"]))["telegram_audio_delivery"] == "pending"
    with patch(
        "agent_backbone.services.integrations.telegram._report_delivery.generate_voice",
        AsyncMock(return_value=b"OggSvoice"),
    ) as generate:
        await bot.flush_report_audio()
    generate.assert_awaited_once()
    voices = bot._app.bot.send_voice.await_args_list
    assert voices[0].kwargs["voice"] == b"OggSvoice"
    assert voices[1].kwargs["voice"] == "telegram-voice-file"
    assert voices[0].kwargs["reply_parameters"].message_id == 41
    assert voices[1].kwargs["reply_parameters"].message_id == 42
    assert voices[1].kwargs["message_thread_id"] == 77
    assert (await db.reports.get(record["id"]))["telegram_audio_delivery"] == "sent"


async def test_audio_failure_never_resends_text_and_partial_voice_uses_file_id(db, config):
    await author(db)
    record, _ = await db.reports.publish(publication())
    bot = configured_bot(config, db)
    await bot.flush_reports()
    with patch(
        "agent_backbone.services.integrations.telegram._report_delivery.generate_voice",
        AsyncMock(side_effect=RuntimeError("TTS unavailable")),
    ):
        await bot.flush_report_audio()
    assert (await db.reports.get(record["id"]))["telegram_delivery"] == "sent"
    assert (await db.reports.get(record["id"]))["telegram_audio_delivery"] == "pending"
    await bot.flush_reports()
    assert bot._app.bot.send_message.await_count == 2
    async with db._engine.begin() as conn:
        await conn.execute(text("UPDATE reports SET telegram_audio_retry_at = ''"))
    bot._app.bot.send_voice.side_effect = [
        SimpleNamespace(message_id=52, voice=SimpleNamespace(file_id="cached")),
        RuntimeError("topic offline"),
    ]
    with patch(
        "agent_backbone.services.integrations.telegram._report_delivery.generate_voice",
        AsyncMock(return_value=b"OggSvoice"),
    ):
        await bot.flush_report_audio()
    async with db._engine.begin() as conn:
        await conn.execute(text("UPDATE reports SET telegram_audio_retry_at = ''"))
    bot._app.bot.send_voice.side_effect = [
        SimpleNamespace(message_id=53, voice=SimpleNamespace(file_id="cached"))
    ]
    with patch(
        "agent_backbone.services.integrations.telegram._report_delivery.generate_voice", AsyncMock()
    ) as generate:
        await bot.flush_report_audio()
    generate.assert_not_awaited()
    assert bot._app.bot.send_voice.await_args.kwargs["voice"] == "cached"
    assert bot._app.bot.send_voice.await_args.kwargs["message_thread_id"] == 77


def test_speech_includes_every_section_and_reference_title():
    request = publication()
    record = {
        "agent_name": "writer",
        "author_name": "writer",
        "report": request.report.model_dump(),
    }
    spoken = spoken_report(record)
    for _, section in request.report.sections():
        assert section.text in spoken
        for link in section.links:
            assert link.title in spoken
            assert link.url not in spoken


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/speak",
        "http://127.0.0.1.evil/speak",
        "http://user:password@localhost/speak",
    ],
)
def test_speech_endpoint_stays_local(url):
    with pytest.raises(ValueError):
        validate_setting("telegram.tts_url", url)
