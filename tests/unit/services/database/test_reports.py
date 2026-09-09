"""Reports survive restarts and retain the right author and page boundaries."""

import asyncio
import base64
import json

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from agent_backbone.models import REPORTS_PER_HOUR, ReportQuery
from agent_backbone.services.database import BackboneDB, ReportConflict, ReportRateLimit
from tests.report_support import author, publication, publish


async def test_publication_idempotency_and_conflicts(db):
    await author(db)
    first, created = await db.reports.publish(publication())
    assert created
    again, created = await db.reports.publish(publication())
    assert not created and first["id"] == again["id"]
    assert first["source"] == "api" and first["author_name"] == "writer"
    changed = publication()
    changed.report.goal.text = "A different goal."
    with pytest.raises(ReportConflict, match="different content"):
        await db.reports.publish(changed)
    unchanged = publication()
    unchanged.request_id = "new-key"
    with pytest.raises(ReportConflict, match="unchanged"):
        await db.reports.publish(unchanged)
    assert len((await db.reports.query(ReportQuery(history=True)))["items"]) == 1


async def test_database_revalidates_and_requires_registered_author(db):
    with pytest.raises(KeyError):
        await db.reports.publish(publication())
    await author(db)
    invalid = publication()
    invalid.report.progress.text = "a" * 481
    with pytest.raises(ValidationError):
        await db.reports.publish(invalid)
    assert not (await db.reports.query(ReportQuery(history=True)))["items"]


async def test_latest_aggregation_happens_before_limit_and_includes_missing(db):
    for name in ("writer", "quiet", "missing"):
        await author(db, name)
    quiet = await publish(db, "quiet")
    for key in ("one", "two", "three"):
        latest = await publish(db, key=key)
    page = await db.reports.query(ReportQuery(limit=2))
    assert [entry["record"]["id"] for entry in page["items"]] == [latest["id"], quiet["id"]]
    page = await db.reports.query(ReportQuery(limit=2, cursor=page["next_cursor"]))
    assert page["items"] == [{"agent_name": "missing", "author_id": None, "record": None}]
    assert not page["has_more"]


async def test_latest_pages_hold_snapshot_when_new_reports_arrive(db):
    for name in ("a", "b", "c"):
        await author(db, name)
        await publish(db, name)
    first = await db.reports.query(ReportQuery(limit=1))
    await publish(db, "b", "new")
    second = await db.reports.query(ReportQuery(limit=1, cursor=first["next_cursor"]))
    assert second["items"][0]["record"]["request_id"] == "first"
    with pytest.raises(ValueError, match="invalid cursor"):
        await db.reports.query(ReportQuery(history=True, cursor=first["next_cursor"]))


async def test_history_pagination_uses_ids_and_excludes_new_publications(db):
    await author(db)
    records = [await publish(db, key=str(n)) for n in range(5)]
    page = await db.reports.query(ReportQuery(history=True, limit=2))
    ids = [entry["record"]["id"] for entry in page["items"]]
    await publish(db, key="new")
    while page["next_cursor"]:
        page = await db.reports.query(
            ReportQuery(history=True, limit=2, cursor=page["next_cursor"])
        )
        ids.extend(entry["record"]["id"] for entry in page["items"])
    assert ids == [r["id"] for r in reversed(records)]


async def test_owner_attention_first_and_member_drilldown(db):
    await author(db)
    await author(db, "coordinator")
    await author(db, "member")
    await publish(
        db, "writer", blockers={"kind": "owner", "text": "Please choose a design.", "links": []}
    )
    await publish(db, "coordinator")
    await publish(db, "member")
    await author(db, "coordinator", tags=("swarm:demo", "role:coordinator"))
    await author(db, "member", tags=("swarm:demo", "role:worker"))
    page = await db.reports.query(ReportQuery())
    assert [entry["agent_name"] for entry in page["items"]] == ["writer"]
    page = await db.reports.query(ReportQuery(agents=["member"]))
    assert [entry["agent_name"] for entry in page["items"]] == ["member"]
    assert len((await db.reports.query(ReportQuery(members=True)))["items"]) == 3
    assert len((await db.reports.query(ReportQuery(history=True)))["items"]) == 1


async def test_rename_and_forget_never_reattribute_old_reports(db):
    await author(db)
    original = await publish(db)
    # The held rename command rekeys the agent row in place. The report author ID
    # survives without mutating any historical report, including its name snapshot.
    async with db.engine.begin() as conn:
        await conn.execute(text("UPDATE agents SET name = 'renamed' WHERE name = 'writer'"))
    old = await db.reports.get(original["id"])
    assert old["agent_name"] == "renamed" and old["author_name"] == "writer"
    assert (await db.reports.query(ReportQuery(agents=["renamed"], history=True)))["items"][0][
        "record"
    ]["id"] == old["id"]
    await db.agents.delete("renamed")
    assert (await db.reports.get(old["id"]))["agent_name"] is None
    await author(db)
    assert (await db.reports.query(ReportQuery(agents=["writer"])))["items"][0]["record"] is None
    fresh = await publish(db)
    assert fresh["author_id"] != old["author_id"]
    archive = await db.reports.query(ReportQuery(history=True, author_id=old["author_id"]))
    assert [entry["record"]["id"] for entry in archive["items"]] == [old["id"]]


async def test_retention_preserves_latest_but_marks_it_old_and_inactive(db):
    await author(db)
    older = await publish(db)
    last = await publish(
        db,
        key="finish",
        status="inactive",
        next={"text": "Nothing active; the work is finished.", "links": []},
    )
    async with db.engine.begin() as conn:
        await conn.execute(text("UPDATE reports SET created_at = '2020-01-01T00:00:00.000000Z'"))
    assert await db.reports.prune(30) == 1
    assert await db.reports.get(older["id"]) is None
    retained = await db.reports.get(last["id"])
    assert retained["stale"] and retained["report"]["status"] == "inactive"


async def test_rate_limit_and_retry_do_not_consume_more_allowance(db):
    await author(db)
    for n in range(REPORTS_PER_HOUR):
        await publish(db, key=str(n))
    with pytest.raises(ReportRateLimit):
        await publish(db, key="too-many")
    assert not (await db.reports.publish(publication(key="0")))[1]


async def test_concurrent_retry_and_restart_keep_one_report(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'reports.db'}"
    async with BackboneDB.connect(url) as db:
        await author(db)
        results = await asyncio.gather(*(db.reports.publish(publication()) for _ in range(5)))
        assert sum(created for _, created in results) == 1
        record_id = results[0][0]["id"]
    async with BackboneDB.connect(url) as db:
        assert (await db.reports.get(record_id))["report"] == publication().report.model_dump()
        assert not (await db.reports.publish(publication()))[1]


@pytest.mark.parametrize("obsolete_stamp", [True, False])
async def test_installed_schema_repair_adds_reporting_without_losing_agents(
    tmp_path, obsolete_stamp
):
    url = f"sqlite+aiosqlite:///{tmp_path / 'installed.db'}"
    async with BackboneDB.connect(url) as db:
        await author(db)
        delivery = await db.deliveries.record(
            issue_number=42, target_entity="writer", session_name="writer", outcome="delivered"
        )
        async with db.engine.begin() as conn:
            await conn.execute(text("DROP TABLE reports"))
            await conn.execute(text("DROP INDEX uq_agents_report_identity"))
            await conn.execute(text("ALTER TABLE agents DROP COLUMN report_identity"))
            if obsolete_stamp:
                await conn.execute(text("UPDATE alembic_version SET version_num = '00d3414fb746'"))
    async with BackboneDB.connect(url) as db:
        assert [agent["name"] for agent in await db.agents.list()] == ["writer"]
        assert (await db.deliveries.query(issue_number=42))[0]["id"] == delivery
        assert (await publish(db))["author_id"]


@pytest.mark.parametrize("field", ["snapshot", "id", "priority", "name"])
async def test_cursor_rejects_changes_to_every_boundary_field(db, field):
    await author(db)
    for key in ("one", "two", "three"):
        await publish(db, key=key)
    page = await db.reports.query(ReportQuery(history=True, limit=1))
    encoded, signature = page["next_cursor"].rsplit(".", 1)
    payload = json.loads(base64.urlsafe_b64decode(encoded))
    payload[field] = "other" if field == "name" else payload[field] + 1
    altered = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode() + "." + signature
    with pytest.raises(ValueError, match="invalid cursor"):
        await db.reports.query(ReportQuery(history=True, limit=1, cursor=altered))


async def test_cursor_expires_after_restart_but_reports_remain(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'cursor.db'}"
    async with BackboneDB.connect(url) as db:
        await author(db)
        await publish(db, key="one")
        await publish(db, key="two")
        page = await db.reports.query(ReportQuery(history=True, limit=1))
    async with BackboneDB.connect(url) as db:
        with pytest.raises(ValueError, match="service restart"):
            await db.reports.query(ReportQuery(history=True, cursor=page["next_cursor"]))
        fresh = await db.reports.query(ReportQuery(history=True))
        assert len(fresh["items"]) == 2
