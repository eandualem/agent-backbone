"""The runtime capability contract matches the registry, the adapters and the docs."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_backbone.services.runtimes import RUNTIMES
from agent_backbone.services.runtimes.capabilities import (
    CAPABILITIES,
    REQUIRED,
    REQUIRED_BASELINE,
    UNAVAILABLE,
    markdown_table,
    unavailable,
)

_ROOT = Path(__file__).parents[4]
_DOC = _ROOT / "docs" / "runtime-capabilities.md"
_BEGIN = "<!-- capability-table:begin -->"
_END = "<!-- capability-table:end -->"
_ROWS = pytest.mark.parametrize("cap", CAPABILITIES, ids=lambda cap: cap.id)


def _cited(reference: str) -> Path | None:
    """A file a reference points at, when it points at one (live receipts do not)."""
    return _ROOT / reference if reference.startswith(("src/", "tests/", "docs/")) else None


def test_every_shipped_adapter_has_a_cell_in_every_row():
    for cap in CAPABILITIES:
        assert set(cap.cells) == set(RUNTIMES), cap.id


def test_the_required_pair_is_shipped():
    assert set(REQUIRED) <= set(RUNTIMES)


# The baseline as the contract shipped it. Entries are only ever removed: a new
# required-pair gap needs the owner's explicit exception (the ``exception`` status),
# never a baseline entry.
INITIAL_BASELINE = {
    ("global-instructions-detected", "claude", 274),
    ("global-instructions-detected", "codex", 274),
    ("cli-memory", "claude", 292),
    ("cli-memory", "codex", 292),
    ("plan-approval", "codex", 278),
    ("refusal-alert", "codex", 279),
    ("browser-group-name", "codex", 270),
    ("bounded-unattended", "claude", 285),
    ("auto-review", "claude", 293),
    ("deep-review", "codex", 288),
}


def test_the_baseline_never_grows():
    assert REQUIRED_BASELINE <= INITIAL_BASELINE


def test_the_required_pair_lacks_nothing_outside_the_shipped_baseline():
    for cap in CAPABILITIES:
        for runtime_id in REQUIRED:
            cell = cap.cells[runtime_id]
            if cell.status not in ("supported", "exception"):
                assert (cap.id, runtime_id, cell.issue) in REQUIRED_BASELINE, (
                    f"{cap.id}/{runtime_id}: Claude Code and Codex must both support it"
                )


def test_the_baseline_lists_only_cells_still_missing():
    rows = {cap.id: cap for cap in CAPABILITIES}
    for row, runtime_id, issue in REQUIRED_BASELINE:
        cell = rows[row].cells[runtime_id]
        assert cell.status not in ("supported", "exception"), f"{row}/{runtime_id}: remove it"
        assert cell.issue == issue, f"{row}/{runtime_id}"


def test_capability_ids_are_unique():
    ids = [cap.id for cap in CAPABILITIES]
    assert len(ids) == len(set(ids))


def test_adapters_declare_only_contract_rows():
    ids = {cap.id for cap in CAPABILITIES}
    for runtime_id, runtime in RUNTIMES.items():
        assert runtime.declared_capabilities <= ids, runtime_id


@_ROWS
def test_every_row_names_its_implementation_and_fallback(cap):
    assert cap.implementation and cap.fallback, cap.id
    if (path := _cited(cap.implementation)) is not None:
        assert path.exists(), f"{cap.id}: {cap.implementation}"


@_ROWS
def test_cells_carry_what_their_status_requires(cap):
    for runtime_id, cell in cap.cells.items():
        where = f"{cap.id}/{runtime_id}"
        if cell.status == "gap":
            assert cell.issue is not None, f"{where}: a gap names its issue"
        if cell.status == "exception":
            assert cell.issue is not None and cell.decision, f"{where}: owner decision and issue"
        if cell.status == "n/a":
            assert cell.note, f"{where}: not applicable says why"
            assert cell.issue is None, f"{where}: an open issue means a gap, not n/a"
        if cell.status == "unverified":
            assert cell.issue is not None, f"{where}: an issue tracks the verification"


@_ROWS
def test_supported_is_claimed_only_where_the_adapter_implements_it(cap):
    for runtime_id, cell in cap.cells.items():
        if cell.status == "supported":
            assert cap.implemented(RUNTIMES[runtime_id]), f"{cap.id}/{runtime_id}"


@_ROWS
def test_an_implemented_capability_is_not_recorded_as_impossible(cap):
    for runtime_id, runtime in RUNTIMES.items():
        if cap.implemented(runtime):
            assert cap.cells[runtime_id].status != "n/a", f"{cap.id}/{runtime_id}"


@_ROWS
def test_every_supported_cell_cites_evidence_for_that_runtime(cap):
    for runtime_id, cell in cap.cells.items():
        if cell.status != "supported":
            continue
        where = f"{cap.id}/{runtime_id}"
        assert cell.evidence, f"{where}: supported needs a behaviour test or live receipt"
        path = _cited(cell.evidence)
        if path is None:
            assert cell.evidence.startswith("live: "), f"{where}: {cell.evidence}"
            continue
        assert path.exists(), f"{where}: {cell.evidence}"
        if path.parts[len(_ROOT.parts)] == "tests":
            assert runtime_id in path.read_text(), f"{where}: {cell.evidence} never names it"


def test_unavailable_lists_gaps_unverified_cells_and_exceptions():
    assert UNAVAILABLE == {"gap", "unverified", "exception"}
    names = {cap.id for cap in unavailable("aider")}
    assert {"approve", "delivery", "brief-at-start"} <= names
    assert "trust" not in {cap.id for cap in unavailable("opencode")}  # n/a is not missing


def test_the_docs_table_is_generated_from_the_contract():
    text = _DOC.read_text()
    table = text.split(_BEGIN, 1)[1].split(_END, 1)[0].strip()
    assert table == markdown_table(tuple(RUNTIMES)), (
        "docs/runtime-capabilities.md is out of date: regenerate its table from "
        "markdown_table(tuple(RUNTIMES))"
    )
