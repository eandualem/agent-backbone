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


async def test_without_a_tmux_server_the_totals_still_count():
    from agent_backbone.services.terminal import CallerUnknown

    with (
        patch(f"{_MEM}._output", AsyncMock(return_value=(0, _PS))),
        patch(f"{_MEM}._panes", AsyncMock(side_effect=CallerUnknown("no tmux server answered"))),
        patch(f"{_MEM}.os.getpid", return_value=300),
    ):
        assert await process_memory() == ({}, 70000, 77500)
