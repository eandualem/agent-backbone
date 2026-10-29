"""Response identity, isolation and retention reuse the durable author store."""

import pytest
from sqlalchemy import text

from agent_backbone.models import PublishResponse
from agent_backbone.services.database import ReportForbidden
from tests.report_support import author, publication
from tests.unit.api.routes.test_api_responses import body


async def test_responses_are_retained_and_correction_is_idempotent(db):
    await author(db)
    first, _ = await db.reports.publish(PublishResponse.model_validate(body(agent="writer")))
    request = PublishResponse.model_validate(
        body(agent="writer", key="corrected", supersedes_id=first["id"])
    )
    corrected, _ = await db.reports.publish(request)
    report, _ = await db.reports.publish(publication())
    async with db.engine.begin() as conn:
        await conn.execute(text("UPDATE reports SET created_at = '2000-01-01T00:00:00Z'"))
    assert await db.reports.get(first["id"], kind="response")
    assert (await db.reports.get(corrected["id"], kind="response"))["conversation_id"] == first[
        "id"
    ]
    assert await db.reports.get(report["id"])
    retry, created = await db.reports.publish(request)
    assert not created and retry["id"] == corrected["id"]


async def test_response_swarm_gate_and_forgotten_author_pending_delivery(db):
    await author(db, tags=("swarm:demo", "role:coordinator"))
    with pytest.raises(ReportForbidden):
        await db.reports.publish(PublishResponse.model_validate(body(agent="writer")))
    await author(db)
    record, _ = await db.reports.publish(PublishResponse.model_validate(body(agent="writer")))
    async with db.engine.begin() as conn:
        await conn.execute(text("DELETE FROM agents WHERE name = 'writer'"))
    assert await db.reports.claim_telegram(include_reports=False) is None
    assert (await db.reports.get(record["id"], kind="response"))[
        "telegram_delivery"
    ] == "not_requested"
    assert (await db.reports.responses())["items"][0]["agent_name"] is None
