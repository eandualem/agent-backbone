"""The shared neutral workbook used by provider and consumer contract fixtures."""

import json
from pathlib import Path
from uuid import uuid4

from agent_backbone.sheets import CreateSheet, EditSheet


def tracker(author="writer"):
    body = json.loads(
        (Path(__file__).parents[1] / "examples/sheets/opportunity-tracker.json").read_text()
    )
    body["author"] = author
    return CreateSheet.model_validate(body)


def edit(base, operations, author="writer", request_id=None):
    return EditSheet.model_validate(
        {
            "author": author,
            "request_id": request_id or uuid4().hex,
            "base_revision_id": base,
            "operations": operations,
        }
    )


def set_cell(body, value, kind="text", row=1, column=0):
    tab = body.workbook.tabs[0]
    return {
        "op": "set_cells",
        "tab_id": tab.tab_id,
        "cells": [
            {
                "row_id": tab.row_ids[row],
                "column_id": tab.column_ids[column],
                "input": {"kind": kind, "value": value},
            }
        ],
    }
