"""The shared Sheets wire types; a literal is never inferred to be a formula."""

from __future__ import annotations

import json
import math
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

SHEET_WIRE_BYTES = 16 * 1024 * 1024
SHEET_EDIT_BYTES = 1024 * 1024
SHEET_METADATA_BYTES = 1024 * 1024
SHEET_RANGE_BYTES = 4 * 1024 * 1024
SHEET_SNAPSHOT_BYTES = 8 * 1024 * 1024
SHEET_STORAGE_BYTES = 512 * 1024 * 1024
CELL_TEXT_BYTES = 16 * 1024
FORMULA_BYTES = 4 * 1024
RESULT_BYTES = 64 * 1024
MAX_CELLS = 50_000
MAX_ROWS = 10_000
MAX_COLUMNS = 256
MAX_TABS = 16
MAX_EDIT_CELLS = 1000

Identity = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
AgentName = Annotated[str, Field(max_length=128, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")]


def utf8_size(value: str, limit: int) -> str:
    if len(value.encode("utf-8")) > limit:
        raise ValueError(f"text exceeds {limit} UTF-8 bytes")
    return value


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TextInput(WireModel):
    kind: Literal["text"]
    value: str

    @field_validator("value")
    @classmethod
    def bounded(cls, value):
        return utf8_size(value, CELL_TEXT_BYTES)


class NumberInput(WireModel):
    kind: Literal["number"]
    value: float

    @field_validator("value", mode="before")
    @classmethod
    def finite(cls, value):
        try:
            valid = type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError("number must be finite, not a boolean")
        return float(value)


class BooleanInput(WireModel):
    kind: Literal["boolean"]
    value: StrictBool


class DateInput(WireModel):
    kind: Literal["date"]
    value: str

    @field_validator("value")
    @classmethod
    def calendar_date(cls, value):
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value or parsed.year < 1900:
            raise ValueError("date must be YYYY-MM-DD in the spreadsheet date range")
        return value


class FormulaInput(WireModel):
    kind: Literal["formula"]
    value: str

    @field_validator("value")
    @classmethod
    def expression(cls, value):
        if not value.startswith("=") or not value[1:].strip():
            raise ValueError("formula must start with = and contain an expression")
        return utf8_size(value, FORMULA_BYTES)


LiteralInput = Annotated[
    TextInput | NumberInput | BooleanInput | DateInput, Field(discriminator="kind")
]
CellInput = Annotated[
    TextInput | NumberInput | BooleanInput | DateInput | FormulaInput,
    Field(discriminator="kind"),
]


class CellPatch(WireModel):
    row_id: Identity
    column_id: Identity
    input: CellInput | None = None
    number_format: str | None = Field(default=None, max_length=128)

    @field_validator("number_format")
    @classmethod
    def printable_format(cls, value):
        if value is not None:
            utf8_size(value, 512)
            if any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError("number format contains control characters")
        return value

    @model_validator(mode="after")
    def has_edit(self):
        if not self.model_fields_set.intersection({"input", "number_format"}):
            raise ValueError("a cell edit needs input or number_format")
        return self


class ViewSort(WireModel):
    column_id: Identity
    direction: Literal["asc", "desc"]


class ViewFilter(WireModel):
    column_id: Identity
    operator: Literal["eq", "ne", "lt", "lte", "gt", "gte", "contains", "is_blank", "is_not_blank"]
    value: LiteralInput | None = None

    @model_validator(mode="after")
    def comparison_value(self):
        blank = self.operator in {"is_blank", "is_not_blank"}
        if blank != (self.value is None):
            raise ValueError("blank filters have no value; other filters require one")
        if self.operator == "contains" and self.value.kind != "text":
            raise ValueError("contains requires a text value")
        return self


class SheetView(WireModel):
    header_row_id: Identity | None = None
    sort: list[ViewSort] = Field(default_factory=list, max_length=MAX_COLUMNS)
    filters: list[ViewFilter] = Field(default_factory=list, max_length=MAX_COLUMNS)


class TabInput(WireModel):
    tab_id: Identity
    name: str = Field(min_length=1, max_length=31)
    row_ids: list[Identity] = Field(min_length=1, max_length=MAX_ROWS)
    column_ids: list[Identity] = Field(min_length=1, max_length=MAX_COLUMNS)
    cells: list[CellPatch] = Field(default_factory=list, max_length=MAX_CELLS)
    view: SheetView = Field(default_factory=SheetView)

    @field_validator("name")
    @classmethod
    def tab_name(cls, value):
        utf8_size(value, 124)
        if not value.strip() or any(c in "[]:*?/\\" or ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("invalid worksheet name")
        if value.startswith("'") or value.endswith("'"):
            raise ValueError("worksheet name cannot begin or end with an apostrophe")
        return value

    @model_validator(mode="after")
    def referenced_ids(self):
        rows, columns = set(self.row_ids), set(self.column_ids)
        if len(rows) != len(self.row_ids) or len(columns) != len(self.column_ids):
            raise ValueError("row and column IDs must be unique")
        seen = set()
        for cell in self.cells:
            key = (cell.row_id, cell.column_id)
            if cell.row_id not in rows or cell.column_id not in columns or key in seen:
                raise ValueError("cell IDs must identify one existing coordinate")
            seen.add(key)
        if self.view.header_row_id is not None and self.view.header_row_id not in rows:
            raise ValueError("view header is not a row in this tab")
        if any(v.column_id not in columns for v in [*self.view.sort, *self.view.filters]):
            raise ValueError("view column is not in this tab")
        if len({v.column_id for v in self.view.sort}) != len(self.view.sort):
            raise ValueError("sort columns must be unique")
        return self


class WorkbookInput(WireModel):
    tabs: list[TabInput] = Field(min_length=1, max_length=MAX_TABS)

    @model_validator(mode="after")
    def workbook_bounds(self):
        if len({t.tab_id for t in self.tabs}) != len(self.tabs):
            raise ValueError("tab IDs must be unique")
        if len({t.name.casefold() for t in self.tabs}) != len(self.tabs):
            raise ValueError("tab names must be unique")
        if sum(len(t.cells) for t in self.tabs) > MAX_CELLS:
            raise ValueError("workbook exceeds populated cell limit")
        if len(self.model_dump_json(exclude_unset=True).encode()) > SHEET_SNAPSHOT_BYTES:
            raise ValueError("workbook exceeds snapshot byte limit")
        return self


class CreateSheet(WireModel):
    author: AgentName
    request_id: Identity
    title: str = Field(min_length=1, max_length=240)
    workbook: WorkbookInput

    @field_validator("title")
    @classmethod
    def printable_title(cls, value):
        utf8_size(value, 960)
        if not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("title must contain printable text")
        return value


class SetCells(WireModel):
    op: Literal["set_cells"]
    tab_id: Identity
    cells: list[CellPatch] = Field(min_length=1, max_length=MAX_EDIT_CELLS)


class AddTab(WireModel):
    op: Literal["add_tab"]
    tab: TabInput


class RenameTab(WireModel):
    op: Literal["rename_tab"]
    tab_id: Identity
    name: str = Field(min_length=1, max_length=31)

    _name = field_validator("name")(TabInput.tab_name.__func__)


class DeleteTab(WireModel):
    op: Literal["delete_tab"]
    tab_id: Identity


class InsertAxes(WireModel):
    op: Literal["insert_rows", "insert_columns", "move_rows", "move_columns"]
    tab_id: Identity
    before_id: Identity | None
    ids: list[Identity] = Field(min_length=1, max_length=MAX_EDIT_CELLS)


class DeleteAxes(WireModel):
    op: Literal["delete_rows", "delete_columns"]
    tab_id: Identity
    ids: list[Identity] = Field(min_length=1, max_length=MAX_EDIT_CELLS)


class SetView(WireModel):
    op: Literal["set_view"]
    tab_id: Identity
    view: SheetView


class CopyCells(WireModel):
    op: Literal["copy_cells"]
    source_tab_id: Identity
    source_row_ids: list[Identity] = Field(min_length=1, max_length=MAX_EDIT_CELLS)
    source_column_ids: list[Identity] = Field(min_length=1, max_length=MAX_COLUMNS)
    target_tab_id: Identity
    target_row_ids: list[Identity] = Field(min_length=1, max_length=MAX_EDIT_CELLS)
    target_column_ids: list[Identity] = Field(min_length=1, max_length=MAX_COLUMNS)

    @model_validator(mode="after")
    def same_shape(self):
        for ids in (
            self.source_row_ids,
            self.source_column_ids,
            self.target_row_ids,
            self.target_column_ids,
        ):
            if len(ids) != len(set(ids)):
                raise ValueError("copy axis IDs must be unique")
        if len(self.source_row_ids) != len(self.target_row_ids) or len(
            self.source_column_ids
        ) != len(self.target_column_ids):
            raise ValueError("copy source and target shapes must match")
        return self


SheetOperation = Annotated[
    SetCells | AddTab | RenameTab | DeleteTab | InsertAxes | DeleteAxes | SetView | CopyCells,
    Field(discriminator="op"),
]


class EditSheet(WireModel):
    author: AgentName | None = None
    request_id: Identity
    base_revision_id: Identity
    operations: list[SheetOperation] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def bounded_changes(self):
        count = 0
        for op in self.operations:
            if isinstance(op, SetCells):
                count += len(op.cells)
            elif isinstance(op, AddTab):
                count += len(op.tab.cells) + len(op.tab.row_ids) + len(op.tab.column_ids)
            elif isinstance(op, (InsertAxes, DeleteAxes)):
                count += len(op.ids)
                if len(set(op.ids)) != len(op.ids):
                    raise ValueError("axis IDs must be unique")
            elif isinstance(op, CopyCells):
                count += len(op.source_row_ids) * len(op.source_column_ids)
            else:
                count += 1
        if count > MAX_EDIT_CELLS:
            raise ValueError("edit exceeds affected cell/axis limit")
        return self


class SheetAnchor(WireModel):
    tab_id: Identity
    row_ids: list[Identity] = Field(min_length=1, max_length=100)
    column_ids: list[Identity] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def bounded_range(self):
        if len(self.row_ids) != len(set(self.row_ids)) or len(self.column_ids) != len(
            set(self.column_ids)
        ):
            raise ValueError("anchor axis IDs must be unique")
        if len(self.row_ids) * len(self.column_ids) > 100:
            raise ValueError("feedback anchor exceeds 100 cells")
        return self


class SheetFeedback(WireModel):
    author: AgentName | None = None
    request_id: Identity
    observed_revision_id: Identity
    kind: Literal["question", "feedback"]
    text: str = Field(min_length=1, max_length=4000)
    anchor: SheetAnchor | None = None

    @field_validator("text")
    @classmethod
    def nonblank(cls, value):
        utf8_size(value, 16_000)
        if not value.strip():
            raise ValueError("feedback text is blank")
        return value


class SheetImport(WireModel):
    author: AgentName
    request_id: Identity
    title: str = Field(min_length=1, max_length=240)
    format: Literal["json", "csv", "tsv", "xlsx"]
    content: str

    _title = field_validator("title")(CreateSheet.printable_title.__func__)

    @field_validator("content")
    @classmethod
    def content_utf8(cls, value):
        return utf8_size(value, SHEET_WIRE_BYTES)


def canonical_json(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def checked_warnings(warnings):
    if not isinstance(warnings, list) or len(warnings) > 8:
        raise ValueError("invalid sheet diagnostics")
    for warning in warnings:
        if not isinstance(warning, str) or any(ord(c) < 32 or ord(c) == 127 for c in warning):
            raise ValueError("invalid sheet diagnostic text")
        utf8_size(warning, 512)
    if len(json.dumps(warnings, ensure_ascii=True).encode()) > 4096:
        raise ValueError("sheet diagnostics exceed byte limit")
    return warnings
