"""Literal, width-aware human output. JSON and raw-content commands bypass this."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Sequence

from rich import box
from rich.cells import cell_len
from rich.console import Console, Group
from rich.table import Table
from rich.text import Text


def clean(value: object) -> str:
    """Keep readable text, never terminal controls; preserve explicit line breaks."""
    return "".join(
        c if c == "\n" or c.isprintable() else " " if c.isspace() else ""
        for c in str(value if value is not None else "")
    )


def console() -> Console:
    """Bind to the current stdout so redirection and test capture work normally."""
    return Console(
        file=sys.stdout,
        highlight=False,
        markup=False,
        color_system=None
        if "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb"
        else "auto",
    )


def plain_output(output: Console) -> bool:
    return not output.is_terminal or "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb"


def record_view(title: str, fields: Iterable[tuple[str, object]], *, width: int) -> Group:
    if width < 40:
        parts = [Text(clean(title), style="bold cyan")]
        for label, value in fields:
            parts.extend([Text(clean(label) + ":", "bold"), Text(clean(value) or "-")])
        return Group(*parts, Text())
    table = Table.grid(padding=(0, 1), expand=True)
    table.add_column(width=max(6, min(18, width // 3)), style="bold", overflow="fold")
    table.add_column(ratio=1, overflow="fold")
    for label, value in fields:
        table.add_row(Text(clean(label) + ":"), Text(clean(value) or "-"))
    return Group(Text(clean(title), style="bold cyan"), table, Text())


def collection_view(
    title: str,
    columns: Sequence[str],
    rows: Iterable[Sequence[object]],
    *,
    width: int,
    plain: bool = False,
    empty: str = "No entries.",
) -> Group:
    """Fold whole values; on narrow screens show each row as labeled details."""
    values = list(rows)
    if not values:
        return Group(Text(clean(title), "bold cyan"), Text(clean(empty)), Text())
    # Don't squeeze five columns into a sidebar. No fields disappear at small widths.
    sizes = [min(len(label), 16) for label in columns]
    sizes[0] = max(sizes[0], min(24, max(cell_len(clean(row[0]).split("\n")[0]) for row in values)))
    minimum = sum(sizes) + len(columns) * 5 + 1
    # Rich measures each Text cell by its longest word, even with overflow=fold.
    # IDs, model names and timestamps can therefore force a table wider than the
    # header-based estimate; Console would silently crop its right-hand columns.
    natural = [
        max(
            sizes[index],
            max(
                (
                    cell_len(word)
                    for value in [label, *(row[index] for row in values)]
                    for word in clean(value).split()
                ),
                default=0,
            ),
        )
        for index, label in enumerate(columns)
    ]
    minimum = max(minimum, sum(natural) + len(columns) * 3 + 1)
    if width < max(48, minimum):
        return Group(
            Text(clean(title), "bold cyan"),
            *(
                record_view(str(row[0]), zip(columns, row, strict=True), width=width)
                for row in values
            ),
        )
    table = Table(
        title=Text(clean(title), "bold"),
        title_justify="left",
        box=box.ASCII if plain else box.ROUNDED,
        border_style="dim",
        header_style="bold cyan",
        expand=True,
        padding=(0, 1),
        show_lines=True,
    )
    for index, label in enumerate(columns):
        table.add_column(
            label,
            style="bold" if index == 0 else "",
            overflow="fold",
            min_width=sizes[index],
        )
    for row in values:
        if len(row) != len(columns):
            raise ValueError("presentation row does not match its columns")
        table.add_row(*(Text(clean(value) or "-") for value in row))
    return Group(table, Text())


def print_table(
    title: str,
    columns: Sequence[str],
    rows: Iterable[Sequence[object]],
    *,
    empty: str = "No entries.",
) -> None:
    output = console()
    output.print(
        collection_view(
            title, columns, rows, width=output.width, plain=plain_output(output), empty=empty
        )
    )


def print_record(title: str, fields: Iterable[tuple[str, object]]) -> None:
    output = console()
    output.print(record_view(title, fields, width=output.width))


def note(message: object, *, style: str = "") -> None:
    console().print(Text(clean(message), style=style))
