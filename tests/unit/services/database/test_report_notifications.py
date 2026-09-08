"""Durable report notification claims, retries, and old-row upgrade behavior."""

from sqlalchemy import text

from tests.report_support import author, publication


async def test_publish_retry_does_not_queue_another_notification(db):
    await author(db)
    record, _ = await db.reports.publish(publication())
    assert record["telegram_delivery"] == "pending"
    claim = await db.reports.claim_telegram()
    assert claim["record"]["id"] == record["id"]
    assert await db.reports.claim_telegram() is None
    assert not await db.reports.finish_telegram(
        record["id"], "wrong-lease", message_id="1:2", attempts=1
    )
    assert await db.reports.finish_telegram(
        record["id"], claim["lease"], message_id="1:2", attempts=1
    )
    again, created = await db.reports.publish(publication())
    assert not created and again["telegram_delivery"] == "sent"
    assert await db.reports.claim_telegram() is None


async def test_failed_and_abandoned_claims_recover(db):
    await author(db)
    record, _ = await db.reports.publish(publication())
    first = await db.reports.claim_telegram()
    await db.reports.finish_telegram(record["id"], first["lease"], message_id=None, attempts=1)
    assert (await db.reports.get(record["id"]))["telegram_delivery"] == "pending"
    assert await db.reports.claim_telegram() is None  # respects retry backoff
    async with db._engine.begin() as conn:
        await conn.execute(text("UPDATE reports SET telegram_retry_at = ''"))
    second = await db.reports.claim_telegram()
    assert second["attempts"] == 2 and second["lease"] != first["lease"]
    # Process died without acknowledgement: let its lease expire.
    async with db._engine.begin() as conn:
        await conn.execute(text("UPDATE reports SET telegram_retry_at = ''"))
    third = await db.reports.claim_telegram()
    assert third["attempts"] == 3
    assert not await db.reports.finish_telegram(
        record["id"], second["lease"], message_id="1:2", attempts=2
    )
    assert await db.reports.finish_telegram(
        record["id"], third["lease"], message_id="1:3", attempts=3
    )


async def test_legacy_rows_default_to_no_broadcast(db):
    async with db._engine.begin() as conn:
        await conn.execute(
            text("""INSERT INTO reports
            (author_id, author_name, request_id, created_at, source, content,
             priority, swarm_member)
            VALUES ('old', 'writer', 'old', '2026-09-08T00:00:00.000000Z', 'api', '{}', 2, 0)""")
        )
        state = await conn.scalar(text("SELECT telegram_delivery FROM reports"))
    assert state == "not_requested"
    assert await db.reports.claim_telegram() is None
