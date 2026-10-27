"""IronCalc adapter. Import this module only inside the bounded worker."""

from __future__ import annotations

import copy
import math
import re
from uuid import uuid4

import ironcalc as ic

from agent_backbone.sheets._errors import SheetError
from agent_backbone.sheets._models import (
    MAX_CELLS,
    RESULT_BYTES,
    EditSheet,
    WorkbookInput,
    canonical_json,
    utf8_size,
)


def canonical(workbook):
    return WorkbookInput.model_validate(workbook).model_dump()


def find_tab(workbook, tab_id):
    for index, tab in enumerate(workbook["tabs"]):
        if tab["tab_id"] == tab_id:
            return index, tab
    raise SheetError("invalid_reference", "worksheet ID is not in this revision")


def coordinate(tab, row_id, column_id):
    try:
        return tab["row_ids"].index(row_id) + 1, tab["column_ids"].index(column_id) + 1
    except ValueError as exc:
        raise SheetError("invalid_reference", "cell axis ID is not in this revision") from exc


_QUOTED = re.compile(r'"(?:[^"]|"")*"')
_RANGE = re.compile(
    r"\$?([A-Z]{1,3})\$?(\d+):\$?([A-Z]{1,3})\$?(\d+)"
    r"|(?<![A-Za-z0-9$])\$?(\d+):\$?(\d+)"
    r"|(?<![A-Za-z0-9$])\$?([A-Z]{1,3}):\$?([A-Z]{1,3})(?![0-9$])"
)


def _column(letters: str) -> int:
    number = 0
    for letter in letters:
        number = number * 26 + ord(letter) - 64
    return number


def range_sizes(formula: str) -> list[tuple[int, int]]:
    """The extent of every range a formula names, outside string literals."""
    sizes = []
    for m in _RANGE.finditer(_QUOTED.sub('""', formula)):
        if m[1]:
            sizes.append((abs(int(m[4]) - int(m[2])), abs(_column(m[3]) - _column(m[1]))))
        elif m[5]:
            sizes.append((abs(int(m[6]) - int(m[5])), -1))
        else:
            sizes.append((-1, abs(_column(m[8]) - _column(m[7]))))
    return sizes


def formulas(workbook) -> dict[tuple[str, str, str], str]:
    return {
        (tab["tab_id"], cell["row_id"], cell["column_id"]): cell["input"]["value"]
        for tab in workbook["tabs"]
        for cell in tab["cells"]
        if (cell.get("input") or {}).get("kind") == "formula"
    }


def cells_by_id(tab):
    return {(c["row_id"], c["column_id"]): c for c in tab["cells"]}


def set_input(model, sheet, row, column, cell):
    value = cell.get("input")
    if value is None:
        model.clear_cell_contents(sheet, row, column)
    else:
        kind, content = value["kind"], value["value"]
        if kind == "formula":
            # External workbook/structured references have no retained local identity.
            if "[" in _QUOTED.sub("", content) or "]" in _QUOTED.sub("", content):
                raise SheetError(
                    "unsupported_formula", "external and structured references are unsupported"
                )
            model.update_cell_with_formula(sheet, row, column, content)
        elif kind == "date":
            from datetime import date

            day = date.fromisoformat(content)
            serial = (day - date(1899, 12, 31)).days + (day >= date(1900, 3, 1))
            model.update_cell_with_number(sheet, row, column, float(serial))
        else:
            method = {"text": "text", "number": "number", "boolean": "bool"}[kind]
            getattr(model, f"update_cell_with_{method}")(sheet, row, column, content)
    style = model.get_cell_style(sheet, row, column)
    style["num_fmt"] = cell.get("number_format") or (
        "yyyy-mm-dd" if value and value["kind"] == "date" else "general"
    )
    model.set_cell_style(sheet, row, column, style)


def build_model(workbook):
    model = ic.create("Workbook", "en", "UTC", "en")
    # Create temporary names first so an input tab called Sheet1 cannot collide.
    temporary = uuid4().hex[:31]
    while temporary.casefold() in {t["name"].casefold() for t in workbook["tabs"]}:
        temporary = uuid4().hex[:31]
    model.rename_sheet(0, temporary)
    for tab in workbook["tabs"]:
        model.add_sheet(tab["name"])
    model.delete_sheet(0)
    for sheet, tab in enumerate(workbook["tabs"]):
        for cell in tab["cells"]:
            row, column = coordinate(tab, cell["row_id"], cell["column_id"])
            set_input(model, sheet, row, column, cell)
    return model


def refresh_formulas(model, workbook):
    for sheet, tab in enumerate(workbook["tabs"]):
        for cell in tab["cells"]:
            if (cell.get("input") or {}).get("kind") == "formula":
                row, column = coordinate(tab, cell["row_id"], cell["column_id"])
                formula = model.get_cell_formula(sheet, row, column)
                if formula is None:
                    raise SheetError("calculation_failed", "structural edit lost a formula")
                cell["input"]["value"] = formula


def result_value(model, sheet, row, column, cell):
    value = model.get_cell_value(sheet, row, column)
    cell_type = model.get_cell_type(sheet, row, column)
    if cell_type == ic.CellType.ErrorValue:
        kind = "error"
    elif (cell.get("input") or {}).get("kind") == "date":
        kind, value = "date", cell["input"]["value"]
    elif model.is_empty_cell(sheet, row, column):
        kind, value = "blank", None
    elif type(value) is bool:
        kind = "boolean"
    elif type(value) in (float, int) and math.isfinite(value):
        kind = "number"
    elif isinstance(value, str):
        kind = "text"
    else:
        raise SheetError("unsupported_result", "spreadsheet result type is unsupported")
    display = model.get_formatted_cell_value(sheet, row, column)
    try:
        if isinstance(value, str):
            utf8_size(value, RESULT_BYTES)
        utf8_size(display, RESULT_BYTES)
    except ValueError as exc:
        raise SheetError("calculation_limit", "cell result exceeds byte limit") from exc
    return {"kind": kind, "value": value}, display


def snapshot(model, workbook, retired):
    workbook = canonical(workbook)
    model.evaluate()
    results = []
    for sheet, tab in enumerate(workbook["tabs"]):
        declared = cells_by_id(tab)
        # Spill output must stay addressable and cannot silently expand the grid.
        for index, row, column in model.get_all_cells():
            if index != sheet:
                continue
            if row > len(tab["row_ids"]) or column > len(tab["column_ids"]):
                raise SheetError(
                    "calculation_limit", "calculation exceeds declared grid dimensions"
                )
            key = tab["row_ids"][row - 1], tab["column_ids"][column - 1]
            declared.setdefault(key, {"row_id": key[0], "column_id": key[1]})
        tab_results = []
        for cell in declared.values():
            row, column = coordinate(tab, cell["row_id"], cell["column_id"])
            value, display = result_value(model, sheet, row, column, cell)
            if value["kind"] != "blank" or cell.get("number_format"):
                tab_results.append(cell | {"value": value, "display": display})
        results.append({"tab_id": tab["tab_id"], "cells": tab_results})
        if sum(len(t["cells"]) for t in results) > MAX_CELLS:
            raise SheetError("calculation_limit", "calculation exceeds populated cell limit")
    result = {
        "workbook": workbook,
        "results": results,
        "retired": retired,
        "calculation_engine": "ironcalc",
        "calculation_version": ic.__version__,
    }
    if len(canonical_json(result)) > 16 * 1024 * 1024:
        raise SheetError("calculation_limit", "stored revision exceeds byte limit")
    return result


def apply_cells(model, workbook, operation):
    sheet, tab = find_tab(workbook, operation["tab_id"])
    cells = cells_by_id(tab)
    seen = set()
    for patch in operation["cells"]:
        key = patch["row_id"], patch["column_id"]
        if key in seen:
            raise SheetError("invalid_edit", "a batch cell is specified twice")
        seen.add(key)
        row, column = coordinate(tab, *key)
        cell = cells.get(key, {"row_id": key[0], "column_id": key[1]}) | patch
        set_input(model, sheet, row, column, cell)
        if cell.get("input") is None and not cell.get("number_format"):
            cells.pop(key, None)
        else:
            cells[key] = cell
    tab["cells"] = list(cells.values())


def edit_axes(model, workbook, operation, retired):
    sheet, tab = find_tab(workbook, operation["tab_id"])
    op, ids = operation["op"], operation["ids"]
    axis = "rows" if op.endswith("rows") else "columns"
    key = "row_ids" if axis == "rows" else "column_ids"
    order = tab[key]
    retired_key = f"{tab['tab_id']}:{axis}"
    old_ids = retired.setdefault(retired_key, [])
    before = operation.get("before_id")
    if before is not None and before not in order:
        raise SheetError("invalid_reference", "insertion target is not an existing axis")
    user = ic.UserModel.from_bytes(model.to_bytes())
    user.pause_evaluation()
    before_move = formulas(workbook) if op.startswith("move") else {}
    if op.startswith("insert"):
        if set(ids).intersection([*order, *old_ids]):
            raise SheetError("reused_identity", "new axes cannot reuse existing or deleted IDs")
        position = order.index(before) if before else len(order)
        getattr(user, f"insert_{axis}")(sheet, position + 1, len(ids))
        order[position:position] = ids
    elif op.startswith("delete"):
        if not set(ids).issubset(order) or len(ids) == len(order):
            raise SheetError("invalid_reference", "delete must leave at least one axis")
        for position in sorted([order.index(i) for i in ids], reverse=True):
            getattr(user, f"delete_{axis}")(sheet, position + 1, 1)
            old_ids.append(order.pop(position))
        cell_key = "row_id" if axis == "rows" else "column_id"
        tab["cells"] = [c for c in tab["cells"] if c[cell_key] not in ids]
        view = tab.get("view", {})
        if axis == "rows" and view.get("header_row_id") in ids:
            view["header_row_id"] = None
        if axis == "columns":
            for rule in ("sort", "filters"):
                view[rule] = [v for v in view.get(rule, []) if v["column_id"] not in ids]
    else:
        if not set(ids).issubset(order):
            raise SheetError("invalid_reference", "move IDs are not existing axes")
        start = order.index(ids[0])
        if order[start : start + len(ids)] != ids or before in ids:
            raise SheetError(
                "invalid_edit", "move requires contiguous canonical IDs and an outside target"
            )
        remaining = [i for i in order if i not in ids]
        position = remaining.index(before) if before else len(remaining)
        if position != start:
            getattr(user, f"move_{axis}")(sheet, start + 1, len(ids), position - start)
        order[:] = remaining[:position] + ids + remaining[position:]
    model = ic.load_from_bytes(user.to_bytes())
    refresh_formulas(model, workbook)
    # The engine can rewrite a range around rows or columns moved within it,
    # which silently changes what it adds up: refuse rather than save that.
    after_move = formulas(workbook)
    for key, formula in before_move.items():
        if range_sizes(formula) != range_sizes(after_move[key]):
            raise SheetError(
                "invalid_edit",
                "this move would change the size of a range a formula uses; "
                "edit the formula or move the rows or columns outside that range",
            )
    return model


def copy_cells(model, workbook, operation):
    source_sheet, source = find_tab(workbook, operation["source_tab_id"])
    target_sheet, target = find_tab(workbook, operation["target_tab_id"])
    source_cells = cells_by_id(source)
    original = ic.UserModel.from_bytes(model.to_bytes())
    original.pause_evaluation()
    copied = []
    # Snapshot every source before writes, including overlapping copies.
    for source_row, target_row in zip(
        operation["source_row_ids"], operation["target_row_ids"], strict=True
    ):
        for source_col, target_col in zip(
            operation["source_column_ids"], operation["target_column_ids"], strict=True
        ):
            row, column = coordinate(source, source_row, source_col)
            destination = coordinate(target, target_row, target_col)
            cell = copy.deepcopy(source_cells.get((source_row, source_col), {"input": None}))
            clipboard = None
            if (cell.get("input") or {}).get("kind") == "formula":
                original.set_selected_sheet(source_sheet)
                original.set_selected_cell(row, column)
                original.set_selected_range(row, column, row, column)
                clipboard = original.copy_to_clipboard()
            copied.append((target_row, target_col, destination, cell, clipboard))
    for row_id, column_id, destination, cell, clipboard in copied:
        if clipboard:
            user = ic.UserModel.from_bytes(model.to_bytes())
            user.pause_evaluation()
            user.set_selected_sheet(target_sheet)
            user.set_selected_cell(*destination)
            user.paste_from_clipboard(
                clipboard["sheet"], tuple(clipboard["range"]), clipboard["data"], False
            )
            model = ic.load_from_bytes(user.to_bytes())
            cell["input"]["value"] = model.get_cell_formula(target_sheet, *destination)
        apply_cells(
            model,
            workbook,
            {
                "tab_id": target["tab_id"],
                "cells": [
                    {
                        "row_id": row_id,
                        "column_id": column_id,
                        "input": cell.get("input"),
                        "number_format": cell.get("number_format"),
                    }
                ],
            },
        )
    return model


def invalidate_tab_references(model, workbook, removed_sheet):
    """IronCalc delete_sheet leaves name references that can reconnect later.

    Let its parser identify references by renaming the tab to a unique marker.
    Rewrite only the resulting marker-qualified A1 references, never user strings.
    """
    existing = canonical_json(workbook).decode()
    marker = "_deleted_" + uuid4().hex[:22]
    while marker in existing:
        marker = "_deleted_" + uuid4().hex[:22]
    model.rename_sheet(removed_sheet, marker)
    cell = r"\$?[A-Z]{1,3}\$?[1-9][0-9]*"
    reference = (
        rf"(?:{cell}(?::{cell})?|\$?[A-Z]{{1,3}}:\$?[A-Z]{{1,3}}|\$?[1-9][0-9]*:\$?[1-9][0-9]*)"
    )
    pattern = re.compile(rf"(?:'{marker}'|{marker})!{reference}(?![A-Za-z0-9_])")
    for sheet, tab in enumerate(workbook["tabs"]):
        if sheet == removed_sheet:
            continue
        for entry in tab["cells"]:
            if (entry.get("input") or {}).get("kind") != "formula":
                continue
            row, column = coordinate(tab, entry["row_id"], entry["column_id"])
            formula = model.get_cell_formula(sheet, row, column)
            if marker not in formula:
                continue
            rewritten = pattern.sub("#REF!", formula)
            if marker in rewritten:
                raise SheetError(
                    "unsupported_reference", "cannot safely invalidate this worksheet reference"
                )
            model.update_cell_with_formula(sheet, row, column, rewritten)


def edit(snapshot_data, edit):
    workbook = copy.deepcopy(snapshot_data["workbook"])
    retired = copy.deepcopy(snapshot_data.get("retired", {}))
    model = build_model(workbook)
    for operation in edit["operations"]:
        op = operation["op"]
        if op == "set_cells":
            apply_cells(model, workbook, operation)
        elif op == "copy_cells":
            model = copy_cells(model, workbook, operation)
        elif op == "add_tab":
            tab = copy.deepcopy(operation["tab"])
            if tab["tab_id"] in retired.get("tabs", []) or any(
                t["tab_id"] == tab["tab_id"] for t in workbook["tabs"]
            ):
                raise SheetError("reused_identity", "worksheet ID was already used")
            workbook["tabs"].append(tab)
            workbook = canonical(workbook)
            tab = workbook["tabs"][-1]
            model.add_sheet(tab["name"])
            for cell in tab["cells"]:
                set_input(
                    model,
                    len(workbook["tabs"]) - 1,
                    *coordinate(tab, cell["row_id"], cell["column_id"]),
                    cell,
                )
        elif op in {"rename_tab", "delete_tab", "set_view"}:
            sheet, tab = find_tab(workbook, operation["tab_id"])
            if op == "rename_tab":
                model.rename_sheet(sheet, operation["name"])
                tab["name"] = operation["name"]
            elif op == "delete_tab":
                if len(workbook["tabs"]) == 1:
                    raise SheetError("invalid_edit", "cannot delete the last worksheet")
                invalidate_tab_references(model, workbook, sheet)
                model.delete_sheet(sheet)
                retired.setdefault("tabs", []).append(tab["tab_id"])
                workbook["tabs"].pop(sheet)
            else:
                tab["view"] = operation["view"]
            refresh_formulas(model, workbook)
        else:
            model = edit_axes(model, workbook, operation, retired)
        workbook = canonical(workbook)
    return snapshot(model, workbook, retired)


def execute(request):
    action = request["action"]
    if action == "create":
        workbook = canonical(request["workbook"])
        return {"snapshot": snapshot(build_model(workbook), workbook, {})}
    if action == "edit":
        parsed = EditSheet.model_validate(request["edit"]).model_dump(exclude_unset=True)
        return {"snapshot": edit(request["snapshot"], parsed)}
    from agent_backbone.sheets._interchange import export_file, import_file

    if action == "import":
        workbook, warnings = import_file(request["format"], request["content"])
        return {"snapshot": snapshot(build_model(workbook), workbook, {}), "warnings": warnings}
    if action == "export":
        return export_file(request["snapshot"], request["format"], request.get("tab_id"))
    raise SheetError("invalid_operation", "unknown calculation operation")
