"""Resident memory of each session's process trees."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from agent_backbone.services.terminal import process_memory

_MEM = "agent_backbone.services.terminal._memory"

_PS = """\
    1     0   1000
  100     1    500
  101   100   2000
  102   101   3000
  200     1    400
  201   200    600
  300     1  70000
"""


async def test_each_session_sums_its_pane_process_trees():
    panes = {100: {"desk", "desk-view"}, 200: {"desk"}, 999: {"gone"}}
    with (
        patch(f"{_MEM}._output", AsyncMock(return_value=(0, _PS))),
        patch(f"{_MEM}._panes", AsyncMock(return_value=panes)),
        patch(f"{_MEM}.os.getpid", return_value=300),
    ):
        sessions, own, total = await process_memory()
    assert sessions == {"desk": (6500, 5), "desk-view": (5500, 3)}
    assert (own, total) == (70000, 77500)


async def test_without_a_tmux_server_there_are_no_sessions_and_the_totals_still_count():
    with (
        patch(f"{_MEM}._output", AsyncMock(return_value=(0, _PS))),
        patch(f"{_MEM}._run_tmux", AsyncMock(return_value=(1, b"", b"no server running on x"))),
        patch(f"{_MEM}.os.getpid", return_value=300),
    ):
        assert await process_memory() == ({}, 70000, 77500)


async def test_when_tmux_cannot_be_read_the_sessions_are_unknown():
    with (
        patch(f"{_MEM}._output", AsyncMock(return_value=(0, _PS))),
        patch(f"{_MEM}._run_tmux", AsyncMock(return_value=(-1, b"", b"tmux command timed out"))),
        patch(f"{_MEM}.os.getpid", return_value=300),
    ):
        assert await process_memory() == (None, 70000, 77500)


async def test_this_process_is_never_counted_in_a_session():
    panes = {100: {"desk"}}
    with (
        patch(f"{_MEM}._output", AsyncMock(return_value=(0, _PS))),
        patch(f"{_MEM}._panes", AsyncMock(return_value=panes)),
        patch(f"{_MEM}.os.getpid", return_value=101),  # the backbone runs in desk's pane
    ):
        sessions, own, _ = await process_memory()
    assert sessions == {"desk": (500, 1)} and own == 2000
