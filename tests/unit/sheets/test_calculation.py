"""Behavior at the real bounded worker boundary, with no service or model calls."""

import base64
from uuid import uuid4

import pytest

from agent_backbone.sheets import SheetCalculator, SheetError, sheet_range
from tests.sheet_support import edit, tracker


async def calculate(body=None):
    body = body or tracker()
    return (
        await SheetCalculator().run({"action": "create", "workbook": body.workbook.model_dump()})
    )["snapshot"]


async def changed(snapshot, operations):
    return (
        await SheetCalculator().run(
            {
                "action": "edit",
                "snapshot": snapshot,
                "edit": edit(uuid4().hex, operations).model_dump(exclude_unset=True),
            }
        )
    )["snapshot"]


async def test_add_empty_tab_with_omitted_defaults():
    tab = {
        "tab_id": uuid4().hex,
        "name": "Empty",
        "row_ids": [uuid4().hex],
        "column_ids": [uuid4().hex],
    }
    result = await changed(await calculate(), [{"op": "add_tab", "tab": tab}])
    assert result["workbook"]["tabs"][-1] == {
        **tab,
        "cells": [],
        "view": {"header_row_id": None, "sort": [], "filters": []},
    }


async def test_xlsx_custom_date_format_warns_about_numeric_import():
    body = tracker()
    cell = next(c for c in body.workbook.tabs[0].cells if c.input.kind == "date")
    cell.number_format = "dd/mm/yyyy"
    calculator = SheetCalculator()
    exported = await calculator.run(
        {"action": "export", "format": "xlsx", "snapshot": await calculate(body)}
    )
    restored = await calculator.run(
        {"action": "import", "format": "xlsx", "content": exported["content"]}
    )
    imported = next(
        c
        for c in restored["snapshot"]["workbook"]["tabs"][0]["cells"]
        if c["number_format"] == "dd/mm/yyyy"
    )
    assert imported["input"]["kind"] == "number"
    assert any("custom date formats remain numbers" in w for w in restored["warnings"])


async def test_typed_literals_cross_tab_formula_error_and_date():
    body = tracker()
    tab = body.workbook.tabs[0]
    raw = body.workbook.model_dump()
    raw["tabs"][0]["cells"].append(
        {
            "row_id": tab.row_ids[-1],
            "column_id": tab.column_ids[0],
            "input": {"kind": "text", "value": "=literal"},
        }
    )
    result = (await SheetCalculator().run({"action": "create", "workbook": raw}))["snapshot"]
    assert result["results"][1]["cells"][0]["value"] == {"kind": "number", "value": 1}
    cells = result["results"][0]["cells"]
    assert cells[-1]["input"] == {"kind": "text", "value": "=literal"}
    assert cells[-1]["value"] == {"kind": "text", "value": "=literal"}
    assert next(c for c in cells if c["value"]["kind"] == "date")["value"]["value"] == "2026-10-27"
    error = await changed(
        result,
        [
            {
                "op": "set_cells",
                "tab_id": tab.tab_id,
                "cells": [
                    {
                        "row_id": tab.row_ids[-1],
                        "column_id": tab.column_ids[0],
                        "input": {"kind": "formula", "value": "=1/0"},
                    }
                ],
            }
        ],
    )
    assert error["results"][0]["cells"][-1]["value"] == {"kind": "error", "value": "#DIV/0!"}


async def test_structural_moves_copy_and_format_patch():
    body = tracker()
    tab = body.workbook.tabs[0]
    base = await calculate(body)
    new_id = uuid4().hex
    inserted = await changed(
        base,
        [{"op": "insert_rows", "tab_id": tab.tab_id, "before_id": tab.row_ids[1], "ids": [new_id]}],
    )
    assert (
        inserted["results"][1]["cells"][0]["input"]["value"] == "=COUNTIF(Opportunities!C3:C5,TRUE)"
    )
    moved = await changed(
        inserted,
        [{"op": "move_rows", "tab_id": tab.tab_id, "before_id": None, "ids": [tab.row_ids[1]]}],
    )
    assert moved["workbook"]["tabs"][0]["row_ids"][-1] == tab.row_ids[1]
    deleted = await changed(moved, [{"op": "delete_rows", "tab_id": tab.tab_id, "ids": [new_id]}])
    with pytest.raises(SheetError, match="reuse"):
        await changed(
            deleted,
            [{"op": "insert_rows", "tab_id": tab.tab_id, "before_id": None, "ids": [new_id]}],
        )
    summary = body.workbook.tabs[1]
    # Cross-tab relative formula copy, into A2: the row-relative source range shifts by one.
    copied = await changed(
        base,
        [
            {
                "op": "copy_cells",
                "source_tab_id": summary.tab_id,
                "source_row_ids": summary.row_ids,
                "source_column_ids": summary.column_ids,
                "target_tab_id": tab.tab_id,
                "target_row_ids": [tab.row_ids[1]],
                "target_column_ids": [tab.column_ids[0]],
            }
        ],
    )
    c = next(
        c
        for c in copied["results"][0]["cells"]
        if c["row_id"] == tab.row_ids[1] and c["column_id"] == tab.column_ids[0]
    )
    assert c["input"]["value"] == "=COUNTIF(Opportunities!C3:C5,TRUE)"
    patched = await changed(
        copied,
        [
            {
                "op": "set_cells",
                "tab_id": tab.tab_id,
                "cells": [
                    {
                        "row_id": c["row_id"],
                        "column_id": c["column_id"],
                        "input": {"kind": "number", "value": 2.75},
                    }
                ],
            }
        ],
    )
    cell = next(
        c2
        for c2 in patched["results"][0]["cells"]
        if c2["row_id"] == c["row_id"] and c2["column_id"] == c["column_id"]
    )
    assert cell["number_format"] == "0" and cell["display"] == "3"
    cleared = await changed(
        patched,
        [
            {
                "op": "set_cells",
                "tab_id": tab.tab_id,
                "cells": [
                    {"row_id": c["row_id"], "column_id": c["column_id"], "number_format": None}
                ],
            }
        ],
    )
    assert (
        next(
            c2
            for c2 in cleared["results"][0]["cells"]
            if c2["row_id"] == c["row_id"] and c2["column_id"] == c["column_id"]
        )["display"]
        == "2.75"
    )


async def test_view_preserves_coordinates_and_overlapping_copy_snapshots():
    body, calculator = tracker(), SheetCalculator()
    tab = body.workbook.tabs[0]
    base = await calculate(body)
    view = {
        "header_row_id": tab.row_ids[0],
        "sort": [{"column_id": tab.column_ids[0], "direction": "desc"}],
        "filters": [
            {
                "column_id": tab.column_ids[2],
                "operator": "eq",
                "value": {"kind": "boolean", "value": False},
            }
        ],
    }
    sorted_data = await changed(base, [{"op": "set_view", "tab_id": tab.tab_id, "view": view}])
    selected = sheet_range(
        sorted_data, sheet_id=uuid4().hex, revision_id=uuid4().hex, tab_id=tab.tab_id, order="view"
    )
    assert [r["row_id"] for r in selected["rows"]] == [
        tab.row_ids[0],
        tab.row_ids[3],
        tab.row_ids[1],
    ]
    assert [r["index"] for r in selected["rows"]] == [1, 4, 2]
    assert sorted_data["results"] == base["results"]
    result = await changed(
        base,
        [
            {
                "op": "copy_cells",
                "source_tab_id": tab.tab_id,
                "source_row_ids": tab.row_ids[1:3],
                "source_column_ids": [tab.column_ids[0]],
                "target_tab_id": tab.tab_id,
                "target_row_ids": tab.row_ids[2:4],
                "target_column_ids": [tab.column_ids[0]],
            }
        ],
    )
    values = {
        c["row_id"]: c["value"]["value"]
        for c in result["results"][0]["cells"]
        if c["column_id"] == tab.column_ids[0]
    }
    assert (
        values[tab.row_ids[2]] == "Community demo"
        and values[tab.row_ids[3]] == "Open tools program"
    )
    calculator._busy = True
    with pytest.raises(SheetError) as busy:
        await calculator.run({"action": "create"})
    assert busy.value.status == 503


async def test_json_xlsx_roundtrip_and_literal_csv():
    base = await calculate()
    calculator = SheetCalculator()
    native = await calculator.run({"action": "export", "format": "json", "snapshot": base})
    restored = await calculator.run(
        {
            "action": "import",
            "format": "json",
            "content": base64.b64decode(native["content"]).decode(),
        }
    )
    assert restored["snapshot"]["workbook"] == base["workbook"]
    xlsx = await calculator.run({"action": "export", "format": "xlsx", "snapshot": base})
    restored = await calculator.run(
        {"action": "import", "format": "xlsx", "content": xlsx["content"]}
    )
    assert restored["snapshot"]["results"][1]["cells"][0]["value"]["value"] == 1
    assert any(c["value"]["kind"] == "date" for c in restored["snapshot"]["results"][0]["cells"])
    assert restored["warnings"]
    csv = await calculator.run(
        {"action": "import", "format": "csv", "content": "name,value\n=1+2,TRUE\n"}
    )
    assert [c["input"]["kind"] for c in csv["snapshot"]["workbook"]["tabs"][0]["cells"]] == [
        "text"
    ] * 4
    assert csv["snapshot"]["results"][0]["cells"][2]["value"]["value"] == "=1+2"


async def test_deadline_kills_worker_and_calculator_recovers(monkeypatch, tmp_path):
    calculator = SheetCalculator()
    monkeypatch.setattr("agent_backbone.sheets._calculation.tempfile.tempdir", str(tmp_path))
    monkeypatch.setattr("agent_backbone.sheets._calculation.WORKER_SECONDS", 0.001)
    with pytest.raises(SheetError) as timeout:
        await calculator.run({"action": "create", "workbook": tracker().workbook.model_dump()})
    assert timeout.value.detail["code"] == "calculation_limit" and not calculator._busy
    assert list(tmp_path.iterdir()) == []
    monkeypatch.setattr("agent_backbone.sheets._calculation.WORKER_SECONDS", 10)
    assert (
        await calculator.run({"action": "create", "workbook": tracker().workbook.model_dump()})
    )["snapshot"]


async def test_native_abort_is_a_refusal_not_a_server_crash(monkeypatch):
    from agent_backbone.sheets import _calculation

    spawn = _calculation.create_subprocess_exec
    children = []

    async def aborting(executable, *args, **kwargs):
        child = await spawn(
            executable,
            "-c",
            "import resource,os; resource.setrlimit(resource.RLIMIT_CORE,(0,0)); os.abort()",
            **kwargs,
        )
        children.append(child)
        return child

    monkeypatch.setattr(_calculation, "create_subprocess_exec", aborting)
    calculator = SheetCalculator()
    with pytest.raises(SheetError) as failed:
        await calculator.run({"action": "create", "workbook": tracker().workbook.model_dump()})
    assert failed.value.detail["code"] == "calculation_failed"
    assert children[0].returncode is not None and not calculator._busy


@pytest.mark.skipif(
    __import__("sys").platform != "darwin", reason="macOS watchdog; Linux uses kernel limits"
)
async def test_real_resident_memory_threshold_kills_worker(monkeypatch):
    from agent_backbone.sheets import _calculation

    spawn = _calculation.create_subprocess_exec
    children = []

    async def tracked(*args, **kwargs):
        child = await spawn(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(_calculation, "create_subprocess_exec", tracked)
    # Exercise real OS memory accounting without allocating hundreds of MiB on the host.
    monkeypatch.setattr(_calculation, "WORKER_MEMORY_BYTES", 1)
    with pytest.raises(SheetError) as refused:
        await SheetCalculator().run(
            {"action": "create", "workbook": tracker().workbook.model_dump()}
        )
    assert refused.value.detail["code"] == "calculation_limit"
    assert children[0].returncode is not None


async def test_deleted_tab_references_cannot_rebind_to_a_reused_name():
    from agent_backbone.sheets import CreateSheet

    first, second = uuid4().hex, uuid4().hex
    row, column = uuid4().hex, uuid4().hex
    formulas = ["=Data!A1", "=SUM(Data!$A$1:$A$2)", '=IF(FALSE,Data!A:A,"Data!A1")']
    summary_rows = [uuid4().hex for _ in formulas]
    body = CreateSheet.model_validate(
        {
            "author": "writer",
            "request_id": uuid4().hex,
            "title": "References",
            "workbook": {
                "tabs": [
                    {
                        "tab_id": first,
                        "name": "Data",
                        "row_ids": [row],
                        "column_ids": [column],
                        "cells": [
                            {
                                "row_id": row,
                                "column_id": column,
                                "input": {"kind": "number", "value": 5},
                            }
                        ],
                    },
                    {
                        "tab_id": second,
                        "name": "Summary",
                        "row_ids": summary_rows,
                        "column_ids": [column],
                        "cells": [
                            {
                                "row_id": r,
                                "column_id": column,
                                "input": {"kind": "formula", "value": f},
                            }
                            for r, f in zip(summary_rows, formulas, strict=True)
                        ],
                    },
                ]
            },
        }
    )
    original = await calculate(body)
    deleted = await changed(original, [{"op": "delete_tab", "tab_id": first}])
    replacement = body.workbook.tabs[0].model_dump() | {"tab_id": uuid4().hex}
    replacement["cells"][0]["input"]["value"] = 99
    reused = await changed(deleted, [{"op": "add_tab", "tab": replacement}])
    cells = reused["results"][0]["cells"]
    assert cells[0]["input"]["value"] == "=#REF!"
    assert cells[1]["input"]["value"] == "=SUM(#REF!)"
    assert cells[0]["value"] == cells[1]["value"] == {"kind": "error", "value": "#REF!"}
    assert cells[2]["input"]["value"] == '=IF(FALSE,#REF!,"Data!A1")'
    assert cells[2]["value"]["value"] == "Data!A1"


def _column_sheet(values, formula):
    """One tab: column A holds ``values`` from row 2, B1 holds ``formula``."""
    from agent_backbone.sheets import CreateSheet

    rows = [uuid4().hex for _ in range(len(values) + 1)]
    columns = [uuid4().hex, uuid4().hex]
    cells = [{"row_id": rows[0], "column_id": columns[1], "input": formula}]
    cells += [
        {"row_id": row, "column_id": columns[0], "input": {"kind": "number", "value": value}}
        for row, value in zip(rows[1:], values, strict=True)
    ]
    tab = {"tab_id": uuid4().hex, "name": "Data", "row_ids": rows, "column_ids": columns}
    body = CreateSheet.model_validate(
        {
            "author": "writer",
            "request_id": uuid4().hex,
            "title": "Totals",
            "workbook": {"tabs": [tab | {"cells": cells}]},
        }
    )
    return body, tab


async def test_a_move_that_would_change_a_summed_range_is_refused():
    body, tab = _column_sheet([2, 3, 4], {"kind": "formula", "value": "=SUM(A2:A4)"})
    base = await calculate(body)
    rows = tab["row_ids"]
    with pytest.raises(SheetError, match="size of a range"):
        await changed(
            base,
            [{"op": "move_rows", "tab_id": tab["tab_id"], "before_id": rows[3], "ids": [rows[1]]}],
        )


async def test_brackets_inside_text_are_not_external_references():
    body, _ = _column_sheet([1], {"kind": "formula", "value": '=IF(TRUE,"[done]","[pending]")'})
    snapshot = await calculate(body)
    values = [cell["value"] for cell in snapshot["results"][0]["cells"]]
    assert {"kind": "text", "value": "[done]"} in values
    external, _ = _column_sheet([1], {"kind": "formula", "value": "=[Book1]Data!A1"})
    with pytest.raises(SheetError, match="external"):
        await calculate(external)
