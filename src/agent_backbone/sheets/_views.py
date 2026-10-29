"""Read stored results without loading an engine or changing historical values."""

from __future__ import annotations

from functools import cmp_to_key

from agent_backbone.sheets._errors import SheetError
from agent_backbone.sheets._models import SHEET_RANGE_BYTES, canonical_json

_BLANK = {"kind": "blank", "value": None}
_RANK = {"blank": 0, "boolean": 1, "number": 2, "date": 3, "text": 4, "error": 5}


def _matches(value, rule):
    op, expected = rule["operator"], rule.get("value")
    if op == "is_blank":
        return value["kind"] == "blank"
    if op == "is_not_blank":
        return value["kind"] != "blank"
    same = value["kind"] == expected["kind"]
    a, b = value["value"], expected["value"]
    if op == "eq":
        return same and a == b
    if op == "ne":
        return not same or a != b
    if op == "contains":
        return same and b.casefold() in a.casefold()
    if not same:
        return False
    if op == "lt":
        return a < b
    if op == "lte":
        return a <= b
    if op == "gt":
        return a > b
    return a >= b


def tab_results(snapshot, tab_id):
    tab = next((t for t in snapshot["workbook"]["tabs"] if t["tab_id"] == tab_id), None)
    if tab is None:
        raise SheetError("not_found", "worksheet not found in revision", 404)
    result = next(t for t in snapshot["results"] if t["tab_id"] == tab_id)
    return tab, {(c["row_id"], c["column_id"]): c for c in result["cells"]}


def view_rows(tab, cells):
    view = tab.get("view", {})
    header = view.get("header_row_id")

    def value(row, column):
        return cells.get((row, column), {}).get("value", _BLANK)

    rows = [
        row
        for row in tab["row_ids"]
        if row != header
        and all(_matches(value(row, rule["column_id"]), rule) for rule in view.get("filters", []))
    ]

    def compare(first, second):
        for rule in view.get("sort", []):
            a, b = value(first, rule["column_id"]), value(second, rule["column_id"])
            av, bv = a["value"], b["value"]
            if a["kind"] == "text":
                av = av.casefold()
            if b["kind"] == "text":
                bv = bv.casefold()
            ak, bk = (_RANK[a["kind"]], av), (_RANK[b["kind"]], bv)
            difference = (ak > bk) - (ak < bk)
            if difference:
                return difference if rule["direction"] == "asc" else -difference
        return 0  # Stable sort preserves canonical order for ties.

    rows.sort(key=cmp_to_key(compare))
    return ([header] if header else []) + rows


def sheet_range(
    snapshot,
    *,
    sheet_id,
    revision_id,
    tab_id,
    row_offset=0,
    row_limit=100,
    column_offset=0,
    column_limit=20,
    order="canonical",
):
    if not (
        row_offset >= 0
        and column_offset >= 0
        and 1 <= row_limit <= 200
        and 1 <= column_limit <= 100
        and row_limit * column_limit <= 2000
        and order in {"canonical", "view"}
    ):
        raise SheetError("invalid_range", "invalid range offsets, limits or order")
    tab, cells = tab_results(snapshot, tab_id)
    rows = view_rows(tab, cells) if order == "view" else tab["row_ids"]
    selected_rows = rows[row_offset : row_offset + row_limit]
    selected_columns = tab["column_ids"][column_offset : column_offset + column_limit]
    indices = {row: i + 1 for i, row in enumerate(tab["row_ids"])}
    result = {
        "sheet_id": sheet_id,
        "revision_id": revision_id,
        "tab_id": tab_id,
        "order": order,
        "total_rows": len(rows),
        "total_columns": len(tab["column_ids"]),
        "rows": [{"row_id": row, "index": indices[row]} for row in selected_rows],
        "columns": [
            {"column_id": column, "index": column_offset + i + 1}
            for i, column in enumerate(selected_columns)
        ],
        "cells": [
            cells[(r, c)] for r in selected_rows for c in selected_columns if (r, c) in cells
        ],
        "next_row_offset": row_offset + row_limit if row_offset + row_limit < len(rows) else None,
    }
    if len(canonical_json(result)) > SHEET_RANGE_BYTES:
        raise SheetError("range_too_large", "request a smaller range at the same revision", 413)
    return result


def feedback_context(observed, current, anchor):
    if anchor is None:
        return {"anchor": None}
    tab, cells = tab_results(observed, anchor["tab_id"])
    if not set(anchor["row_ids"]).issubset(tab["row_ids"]) or not set(
        anchor["column_ids"]
    ).issubset(tab["column_ids"]):
        raise SheetError("invalid_reference", "feedback axes are absent from the observed revision")
    current_tab = next(
        (t for t in current["workbook"]["tabs"] if t["tab_id"] == anchor["tab_id"]), None
    )
    values = []
    for row in anchor["row_ids"]:
        for column in anchor["column_ids"]:
            cell = cells.get(
                (row, column), {"row_id": row, "column_id": column, "value": _BLANK, "display": ""}
            )
            values.append(
                cell
                | {
                    "exists_now": bool(
                        current_tab
                        and row in current_tab["row_ids"]
                        and column in current_tab["column_ids"]
                    )
                }
            )
    result = {"anchor": anchor, "tab_name": tab["name"], "cells": values}
    if len(canonical_json(result)) > 128 * 1024:
        raise SheetError("feedback_too_large", "select fewer cells for feedback", 413)
    return result
