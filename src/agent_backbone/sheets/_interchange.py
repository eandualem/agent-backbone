"""Bounded interchange. Native JSON is the lossless format for the supported model."""

from __future__ import annotations

import base64
import csv
import io
import json
import re
import tempfile
import zipfile
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree as ET

import ironcalc as ic

from agent_backbone.sheets._engine import build_model, canonical, find_tab
from agent_backbone.sheets._errors import SheetError
from agent_backbone.sheets._models import MAX_COLUMNS, MAX_ROWS, SHEET_WIRE_BYTES, canonical_json

XLSX_BYTES = 8 * 1024 * 1024
UNPACKED_BYTES = 64 * 1024 * 1024
_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


class _NoDTDTreeBuilder(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        raise SheetError("unsupported_import", "XML entity declarations are unsupported")


def _tab(name, rows, columns):
    if rows > MAX_ROWS or columns > MAX_COLUMNS:
        raise SheetError("import_limit", "imported worksheet exceeds grid dimensions")
    return {
        "tab_id": uuid4().hex,
        "name": name,
        "row_ids": [uuid4().hex for _ in range(max(1, rows))],
        "column_ids": [uuid4().hex for _ in range(max(1, columns))],
        "cells": [],
        "view": {},
    }


def _checked_zip(raw):
    if len(raw) > XLSX_BYTES:
        raise SheetError("import_limit", "XLSX exceeds 8 MiB compressed limit")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
        entries = archive.infolist()
        if len(entries) > 2048 or sum(e.file_size for e in entries) > UNPACKED_BYTES:
            raise SheetError("import_limit", "XLSX exceeds decompressed admission limit")
        names = [e.filename for e in entries]
        if len(set(names)) != len(names):
            raise SheetError("unsupported_import", "XLSX contains duplicate archive entries")
        actual = 0
        warnings = set()
        for entry in entries:
            name = entry.filename.lower()
            if entry.flag_bits & 1 or any(
                part in name
                for part in (
                    "vbaproject",
                    "externallink",
                    "connections",
                    "querytable",
                    "pivot",
                    "charts/",
                    "drawings/",
                    "tables/",
                    "embeddings/",
                    "activex",
                )
            ):
                raise SheetError(
                    "unsupported_import",
                    "encrypted, macro, external, chart, pivot and embedded content is unsupported",
                )
            data = bytearray()
            # Count bytes actually streamed, independently of the ZIP header sizes.
            with archive.open(entry) as source:
                while chunk := source.read(65536):
                    actual += len(chunk)
                    if actual > UNPACKED_BYTES:
                        raise SheetError(
                            "import_limit", "XLSX exceeds actual decompressed byte limit"
                        )
                    if name.endswith((".xml", ".rels")):
                        data.extend(chunk)
            if data:
                root = ET.fromstring(data, parser=ET.XMLParser(target=_NoDTDTreeBuilder()))
                for element in root.iter():
                    tag = element.tag.rsplit("}", 1)[-1]
                    if element.attrib.get("TargetMode") == "External":
                        raise SheetError(
                            "unsupported_import", "external workbook resources are unsupported"
                        )
                    if tag in {
                        "mergeCells",
                        "conditionalFormatting",
                        "dataValidations",
                        "oleObjects",
                        "definedName",
                    }:
                        raise SheetError("unsupported_import", f"XLSX feature {tag} is unsupported")
                    if tag == "extLst" and name.startswith("xl/worksheets/"):
                        raise SheetError(
                            "unsupported_import", "worksheet extensions are unsupported"
                        )
                    if tag in {
                        "sheetView",
                        "cols",
                        "autoFilter",
                        "sheetProtection",
                        "printOptions",
                        "pageSetup",
                    }:
                        warnings.add(
                            "XLSX layout, filters, protection and print "
                            "settings are not retained; create a saved sheet view after import."
                        )
                if name == "xl/workbook.xml":
                    for element in root.iter():
                        if element.tag.endswith("workbookPr") and element.attrib.get(
                            "date1904"
                        ) in {"1", "true"}:
                            raise SheetError(
                                "unsupported_import", "1904-date-system workbooks are unsupported"
                            )
        return sorted(warnings)
    except (zipfile.BadZipFile, ET.ParseError, RuntimeError, OSError) as exc:
        raise SheetError("invalid_import", "XLSX archive is invalid or encrypted") from exc


def _xlsx(raw):
    warnings = _checked_zip(raw)
    with tempfile.TemporaryDirectory(prefix="backbone-sheet-") as directory:
        path = Path(directory) / "import.xlsx"
        path.write_bytes(raw)
        model = ic.load_from_xlsx(str(path))
    tabs = []
    all_cells = model.get_all_cells()
    for index, properties in enumerate(model.get_worksheets_properties()):
        sheet_cells = [(r, c) for s, r, c in all_cells if s == index]
        tab = _tab(
            properties["name"],
            max((r for r, _ in sheet_cells), default=1),
            max((c for _, c in sheet_cells), default=1),
        )
        for row, column in sheet_cells:
            formula = model.get_cell_formula(index, row, column)
            value = model.get_cell_value(index, row, column)
            style = model.get_cell_style(index, row, column)
            number_format = style.get("num_fmt", "general")
            if formula is not None:
                kind, value = "formula", formula
            elif model.is_empty_cell(index, row, column):
                kind = None
            elif model.get_cell_type(index, row, column) == ic.CellType.ErrorValue:
                raise SheetError(
                    "unsupported_import", "literal error cells require a formula or text input"
                )
            elif type(value) is bool:
                kind = "boolean"
            elif type(value) in (int, float):
                kind = "number"
                if (
                    re.fullmatch(r"(?i)(yyyy-mm-dd|mm-dd-yy|m/d/yy|mm/dd/yyyy)", number_format)
                    and value == int(value)
                    and value >= 1
                    and value != 60
                ):
                    value = (
                        date(1899, 12, 31) + timedelta(days=int(value) - (value > 60))
                    ).isoformat()
                    kind = "date"
            elif isinstance(value, str):
                kind = "text"
            else:
                raise SheetError("unsupported_import", "imported cell type is unsupported")
            tab["cells"].append(
                {
                    "row_id": tab["row_ids"][row - 1],
                    "column_id": tab["column_ids"][column - 1],
                    "input": {"kind": kind, "value": value} if kind else None,
                    "number_format": number_format if number_format.lower() != "general" else None,
                }
            )
        tabs.append(tab)
    warnings.append(
        "XLSX preserves supported cell inputs, formulas and number formats; "
        "other styles, view state and stable Backbone IDs are not retained."
    )
    warnings.append(
        "XLSX infers dates only from whole-day values formatted yyyy-mm-dd, mm-dd-yy, "
        "m/d/yy or mm/dd/yyyy. Other custom date formats remain numbers; "
        "native JSON preserves typed dates."
    )
    return canonical({"tabs": tabs}), warnings


def import_file(format, content):
    if format == "json":
        return canonical(json.loads(content)), []
    if format == "xlsx":
        try:
            raw = base64.b64decode(content, validate=True)
        except ValueError as exc:
            raise SheetError("invalid_import", "XLSX content must be valid base64") from exc
        return _xlsx(raw)
    if format not in {"csv", "tsv"}:
        raise SheetError("invalid_import", "unsupported import format")
    csv.field_size_limit(16 * 1024)
    rows = []
    for row in csv.reader(
        io.StringIO(content), delimiter="," if format == "csv" else "\t", strict=True
    ):
        if len(rows) >= MAX_ROWS or len(row) > MAX_COLUMNS:
            raise SheetError("import_limit", "delimited import exceeds grid dimensions")
        rows.append(row)
    tab = _tab("Sheet1", len(rows), max(map(len, rows), default=1))
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            if value:
                tab["cells"].append(
                    {
                        "row_id": tab["row_ids"][r],
                        "column_id": tab["column_ids"][c],
                        "input": {"kind": "text", "value": value},
                    }
                )
    return canonical({"tabs": [tab]}), [
        "Delimited input is literal text in one tab; no types, formulas, "
        "formatting or views are inferred."
    ]


def export_file(snapshot, format, tab_id):
    workbook = snapshot["workbook"]
    warnings = []
    if format == "json":
        raw, mime = canonical_json(workbook), "application/json"
    elif format in {"csv", "tsv"}:
        _, tab = find_tab(workbook, tab_id)
        results = next(t["cells"] for t in snapshot["results"] if t["tab_id"] == tab_id)
        values = {(c["row_id"], c["column_id"]): c["value"]["value"] for c in results}
        output = io.StringIO()
        writer = csv.writer(output, delimiter="," if format == "csv" else "\t")
        for row in tab["row_ids"]:
            writer.writerow([values.get((row, column), "") for column in tab["column_ids"]])
            if output.tell() > SHEET_WIRE_BYTES:
                raise SheetError("export_limit", "export exceeds byte limit")
        raw = output.getvalue().encode()
        mime = "text/csv" if format == "csv" else "text/tab-separated-values"
        warnings = [
            "Single-tab values only. Import formula-like text as text in "
            "other spreadsheet applications."
        ]
    elif format == "xlsx":
        model = build_model(workbook)
        model.evaluate()  # IronCalc requires populated caches before XLSX serialization.
        with tempfile.TemporaryDirectory(prefix="backbone-sheet-") as directory:
            path = Path(directory) / "export.xlsx"
            model.save_to_xlsx(str(path))
            raw = _stored_formula_caches(path.read_bytes(), snapshot)
        mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        warnings = [
            "XLSX does not retain Backbone IDs, saved views or revision "
            "history. Formula caches may be recalculated by the receiving application."
        ]
    else:
        raise SheetError("invalid_export", "unsupported export format")
    if len(raw) > SHEET_WIRE_BYTES:
        raise SheetError("export_limit", "export exceeds byte limit")
    # Parent emits bytes with MIME and bounded diagnostics, never an inline active page.
    return {
        "content": base64.b64encode(raw).decode(),
        "mime": mime,
        "filename": f"workbook.{format}",
        "warnings": warnings,
    }


def _stored_formula_caches(raw, snapshot):
    """Export the revision's original values, even for volatile formulas."""
    output = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(raw)) as source,
        zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for entry in source.infolist():
            data = source.read(entry)
            match = re.fullmatch(r"xl/worksheets/sheet(\d+)\.xml", entry.filename)
            if match:
                index = int(match[1]) - 1
                tab = snapshot["workbook"]["tabs"][index]
                values = {
                    (c["row_id"], c["column_id"]): c["value"]
                    for c in snapshot["results"][index]["cells"]
                }
                root = ET.fromstring(data)
                for cell in root.iter(f"{{{_NS}}}c"):
                    if cell.find(f"{{{_NS}}}f") is None:
                        continue
                    address = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", cell.attrib["r"])
                    column = 0
                    for letter in address[1]:
                        column = column * 26 + ord(letter) - ord("A") + 1
                    key = tab["row_ids"][int(address[2]) - 1], tab["column_ids"][column - 1]
                    value = values.get(key, {"kind": "blank", "value": None})
                    cache = cell.find(f"{{{_NS}}}v")
                    if cache is None:
                        cache = ET.SubElement(cell, f"{{{_NS}}}v")
                    kind, content = value["kind"], value["value"]
                    cell.attrib["t"] = {"text": "str", "error": "e", "boolean": "b"}.get(kind, "n")
                    cache.text = (
                        ""
                        if content is None
                        else str(int(content))
                        if kind == "boolean"
                        else str(content)
                    )
                data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            target.writestr(entry, data)
    return output.getvalue()
