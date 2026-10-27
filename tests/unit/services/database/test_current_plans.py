"""Current-plan serialization, stable authors and content retention."""

import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import text

from agent_backbone.models import PublishCurrentPlan
from agent_backbone.services.database import BackboneDB, CurrentPlanError
from tests.report_support import author
from tests.unit.api.routes.test_api_current_plans import publication


async def test_concurrent_replacements_and_retry_survive_restart(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'plans.db'}"
    async with BackboneDB.connect(url) as db:
        await author(db, "ike")
        identity = (await db.current_plans.query(agent="ike"))["items"][0]["author_id"]
        body = PublishCurrentPlan.model_validate(publication(identity))
        results = await asyncio.gather(*(db.current_plans.publish(body) for _ in range(3)))
        assert sum(r["created"] for r in results) == 1
        receipt = results[0]["receipt"]
        left = body.model_copy(deep=True)
        right = body.model_copy(deep=True)
        for item in (left, right):
            item.request_id = uuid4().hex
            item.base_revision_id = receipt["revision_id"]
        results = await asyncio.gather(
            db.current_plans.publish(left), db.current_plans.publish(right), return_exceptions=True
        )
        assert sum(isinstance(r, CurrentPlanError) for r in results) == 1
        assert sum(isinstance(r, dict) for r in results) == 1
    async with BackboneDB.connect(url) as db:
        assert (await db.current_plans.publish(body))["receipt"] == receipt


async def test_rename_reuse_and_current_only_retention(db):
    await author(db, "ike")
    identity = (await db.current_plans.query(agent="ike"))["items"][0]["author_id"]
    body = publication(identity)
    first = await db.current_plans.publish(PublishCurrentPlan.model_validate(body))
    async with db.engine.begin() as conn:
        await conn.execute(text("UPDATE agents SET name='renamed' WHERE name='ike'"))
    assert (await db.current_plans.get(identity))["agent_name"] == "renamed"
    with pytest.raises(CurrentPlanError, match="registration"):
        await db.current_plans.publish(PublishCurrentPlan.model_validate(body))
    changed = deepcopy(body)
    changed.update(
        author="renamed", request_id=uuid4().hex, base_revision_id=first["receipt"]["revision_id"]
    )
    changed["plan"]["items"] = []
    await db.current_plans.publish(PublishCurrentPlan.model_validate(changed))
    async with db.engine.begin() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM current_plans")) == 1
        receipts = (
            (await conn.execute(text("SELECT receipt FROM current_plan_requests"))).scalars().all()
        )
        assert all("Check the change" not in value for value in receipts)
        await conn.execute(
            text("UPDATE current_plan_requests SET created_at='2020-01-01T00:00:00Z'")
        )
    assert await db.current_plans.prune(30) == 2
    assert (await db.current_plans.get(identity))["plan"]["items"] == []
    await db.agents.delete("renamed")
    await author(db, "ike")
    assert (await db.current_plans.query(agent="ike"))["items"][0]["record"] is None
    with pytest.raises(CurrentPlanError):
        await db.current_plans.publish(PublishCurrentPlan.model_validate(body))
    assert await db.current_plans.prune(30) == 1
