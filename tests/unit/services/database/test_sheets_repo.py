"""Save conflicts, registration ownership and feedback admission are transactional."""

import asyncio
import copy
from uuid import uuid4

import pytest
from sqlalchemy import text

from agent_backbone.services.database import BackboneDB
from agent_backbone.sheets import SheetError, SheetFeedback
from tests.report_support import author
from tests.sheet_support import edit, set_cell, tracker


async def test_revisions_exact_retries_stale_edits_and_registration_reuse(tmp_path):
    path = f"sqlite+aiosqlite:///{tmp_path}/sheet.db"
    async with BackboneDB.connect(path) as db:
        await author(db)
        body = tracker()
        first = (await db.sheets.mutate(body))["record"]
        change = edit(first["revision_id"], [set_cell(body, "Updated")])
        second = (await db.sheets.mutate(change, sheet_id=first["sheet_id"]))["record"]
        retry = await db.sheets.mutate(body)
        assert not retry["created"] and retry["record"]["revision_id"] == first["revision_id"]
        assert retry["record"]["current_revision_id"] == second["revision_id"]
        assert not (await db.sheets.mutate(change, sheet_id=first["sheet_id"]))["created"]
        with pytest.raises(SheetError) as stale:
            await db.sheets.mutate(
                edit(first["revision_id"], [set_cell(body, "Stale")]), sheet_id=first["sheet_id"]
            )
        assert (
            stale.value.status == 409
            and stale.value.detail["current_revision_id"] == second["revision_id"]
        )
        with pytest.raises(SheetError) as changed:
            await db.sheets.mutate(
                change.model_copy(
                    update={
                        "operations": edit(
                            first["revision_id"], [set_cell(body, "Different")]
                        ).operations
                    }
                ),
                sheet_id=first["sheet_id"],
            )
        assert changed.value.detail["code"] == "request_conflict"
        history = await db.sheets.query(sheet_id=first["sheet_id"], limit=1)
        older = await db.sheets.query(
            sheet_id=first["sheet_id"], limit=1, cursor=history["next_cursor"]
        )
        assert older["items"][0]["revision_id"] == first["revision_id"] and not older["next_cursor"]
        with pytest.raises(SheetError):
            await db.sheets.query(cursor=history["next_cursor"])
        await db.agents.rename("writer", "editor")
        assert (await db.sheets.get(first["sheet_id"]))["agent_name"] == "editor"
        await db.agents.delete("editor")
        await author(db, "editor")
        assert (await db.sheets.get(first["sheet_id"]))["agent_name"] is None
        with pytest.raises(SheetError) as replaced:
            await db.sheets.mutate(
                edit(second["revision_id"], [set_cell(body, "Hijack")], "editor"),
                sheet_id=first["sheet_id"],
            )
        assert replaced.value.status == 403
    async with BackboneDB.connect(path) as db:
        original = (await db.sheets.stored(first["sheet_id"], first["revision_id"]))[1]
        assert original["workbook"]["tabs"][0]["cells"][6]["input"]["value"] == "Community demo"


async def test_cas_after_calculation_and_capacity_rollback(tmp_path, monkeypatch):
    async with BackboneDB.connect(f"sqlite+aiosqlite:///{tmp_path}/race.db") as db:
        await author(db)
        body = tracker()
        first = (await db.sheets.mutate(body))["record"]
        base_snapshot = (await db.sheets.stored(first["sheet_id"]))[1]
        entered, release = asyncio.Event(), asyncio.Event()
        calculator = db.sheets.calculator.run

        async def delayed(request):
            if request["edit"]["operations"][0]["cells"][0]["input"]["value"] == "Slow":
                entered.set()
                await release.wait()
            return {"snapshot": copy.deepcopy(base_snapshot)}

        monkeypatch.setattr(db.sheets.calculator, "run", delayed)
        pending = asyncio.create_task(
            db.sheets.mutate(
                edit(first["revision_id"], [set_cell(body, "Slow")]), sheet_id=first["sheet_id"]
            )
        )
        await entered.wait()
        winner = await db.sheets.mutate(
            edit(first["revision_id"], [set_cell(body, "Fast")]), sheet_id=first["sheet_id"]
        )
        release.set()
        with pytest.raises(SheetError) as lost:
            await pending
        assert lost.value.detail["code"] == "stale_revision"
        assert len((await db.sheets.query(sheet_id=first["sheet_id"]))["items"]) == 2
        monkeypatch.setattr(db.sheets.calculator, "run", calculator)
        monkeypatch.setattr("agent_backbone.services.database._sheets_repo.SHEET_STORAGE_BYTES", 1)
        with pytest.raises(SheetError) as full:
            await db.sheets.mutate(
                edit(winner["record"]["revision_id"], [set_cell(body, "Full")]),
                sheet_id=first["sheet_id"],
            )
        assert full.value.status == 507
        assert (await db.sheets.get(first["sheet_id"]))["revision_id"] == winner["record"][
            "revision_id"
        ]
        assert not (await db.sheets.mutate(body))["created"]


async def test_feedback_pins_deleted_row_and_has_one_registration_bound_queue_row(tmp_path):
    async with BackboneDB.connect(f"sqlite+aiosqlite:///{tmp_path}/feedback.db") as db:
        await author(db)
        body = tracker()
        first = (await db.sheets.mutate(body))["record"]
        tab = body.workbook.tabs[0]
        await db.sheets.mutate(
            edit(
                first["revision_id"],
                # Inside the summary's counted range, not one of its ends.
                [{"op": "delete_rows", "tab_id": tab.tab_id, "ids": [tab.row_ids[2]]}],
            ),
            sheet_id=first["sheet_id"],
        )
        request = SheetFeedback(
            author=None,
            request_id=uuid4().hex,
            observed_revision_id=first["revision_id"],
            kind="question",
            text="What happened here?",
            anchor={
                "tab_id": tab.tab_id,
                "row_ids": [tab.row_ids[2]],
                "column_ids": [tab.column_ids[0]],
            },
        )

        def wrap(author, message):
            return f"[via:backbone from:{author}] {message}"

        feedback = await db.sheets.feedback(first["sheet_id"], request, wrap)
        assert (await db.sheets.feedback(first["sheet_id"], request, wrap))[
            "operation_id"
        ] == feedback["operation_id"]
        import json

        context = json.loads(feedback["context"])
        assert context["cells"][0]["exists_now"] is False
        assert context["cells"][0]["value"]["value"] == "Open tools program"
        async with db.engine.connect() as conn:
            queue = (await conn.execute(text("SELECT * FROM message_queue"))).mappings().all()
            assert len(queue) == 1 and queue[0]["registration_id"] == first["author_id"]
        await db.agents.delete("writer")
        await author(db)
        with pytest.raises(SheetError) as replaced:
            await db.sheets.feedback(
                first["sheet_id"], request.model_copy(update={"request_id": uuid4().hex}), wrap
            )
        assert replaced.value.detail["code"] == "owner_unavailable"
        # Original feedback is still retrievable as an idempotent receipt, never re-enqueued.
        assert (await db.sheets.feedback(first["sheet_id"], request, wrap))[
            "feedback_id"
        ] == feedback["feedback_id"]
