"""Synthetic reports and authors shared by API, persistence and Telegram tests."""

from agent_backbone.models import PublishReport, report_example


def publication(agent="writer", key="first", **changes):
    report = report_example()
    report["progress"]["text"] = f"Customers can now complete the checkout step: {key}."
    report.update(changes)
    return PublishReport(agent=agent, request_id=key, report=report)


async def author(db, name="writer", *, tags=()):
    await db.agents.upsert(
        name,
        dir=f"/example/{name}",
        runtime="shell",
        model=None,
        repo="example/shop",
        tags=list(tags),
        env={},
        description="",
        unattended=False,
    )


async def publish(db, name="writer", key="first", **changes):
    record, _ = await db.reports.publish(publication(name, key, **changes))
    return record
