"""The runtime capability contract matches the registry, the adapters and the docs."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_backbone.services.runtimes import RUNTIMES
from agent_backbone.services.runtimes.capabilities import CAPABILITIES, markdown_table, unavailable

_DOC = Path(__file__).parents[4] / "docs" / "runtime-capabilities.md"
_BEGIN = "<!-- capability-table:begin -->"
_END = "<!-- capability-table:end -->"


def test_every_capability_has_one_cell_per_shipped_adapter():
    for cap in CAPABILITIES:
        assert set(cap.cells) == set(RUNTIMES), cap.id


def test_capability_ids_are_unique():
    ids = [cap.id for cap in CAPABILITIES]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("cap", CAPABILITIES, ids=lambda cap: cap.id)
def test_cells_carry_what_their_status_requires(cap):
    for runtime_id, cell in cap.cells.items():
        where = f"{cap.id}/{runtime_id}"
        if cell.status == "gap":
            assert cell.issue is not None, f"{where}: a gap names its issue"
        if cell.status == "n/a":
            assert cell.note, f"{where}: not applicable says why"
            assert cell.issue is None, f"{where}: an open issue means a gap, not n/a"


@pytest.mark.parametrize(
    "cap", [cap for cap in CAPABILITIES if cap.implemented], ids=lambda cap: cap.id
)
def test_supported_is_claimed_only_where_the_adapter_implements_it(cap):
    for runtime_id, cell in cap.cells.items():
        if cell.status == "supported":
            assert cap.implemented(RUNTIMES[runtime_id]), f"{cap.id}/{runtime_id}"


@pytest.mark.parametrize(
    "cap", [cap for cap in CAPABILITIES if cap.implemented], ids=lambda cap: cap.id
)
def test_an_implemented_capability_is_not_left_off_the_contract(cap):
    for runtime_id, runtime in RUNTIMES.items():
        if cap.implemented(runtime):
            assert cap.cells[runtime_id].status != "n/a", f"{cap.id}/{runtime_id}"


def test_unavailable_lists_gaps_and_unverified_cells():
    names = {cap.id for cap in unavailable("aider")}
    assert "approve" in names and "delivery" in names
    assert "brief-at-start" in names


def test_the_docs_table_is_generated_from_the_contract():
    text = _DOC.read_text()
    table = text.split(_BEGIN, 1)[1].split(_END, 1)[0].strip()
    assert table == markdown_table(tuple(RUNTIMES)), (
        "docs/runtime-capabilities.md is out of date: regenerate its table from "
        "markdown_table(tuple(RUNTIMES))"
    )
