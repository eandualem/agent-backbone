"""Resident memory of each tmux session's processes (``ps``, ``tmux list-panes``)."""

from __future__ import annotations

import os

from agent_backbone.services.terminal._callers import CallerUnknown, _output, _panes


async def process_memory() -> tuple[dict[str, tuple[int, int]], int, int]:
    """Resident memory in KB: per session, the summed process trees of its
    panes with their process count; this process; and every process.

    Resident memory leaves out what the system compressed or swapped."""
    code, out = await _output("ps", "-A", "-o", "pid=,ppid=,rss=")
    if code != 0:
        raise CallerUnknown(f"ps failed (exit {code})")
    rss: dict[int, int] = {}
    children: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and all(part.isdigit() for part in parts):
            pid, ppid, kb = (int(part) for part in parts)
            rss[pid] = kb
            children.setdefault(ppid, []).append(pid)
    try:
        panes = await _panes()
    except CallerUnknown:
        panes = {}  # no tmux server: no sessions, the totals still count
    sessions: dict[str, tuple[int, int]] = {}
    for pane, names in panes.items():
        tree, pending = set(), [pane]
        while pending:
            pid = pending.pop()
            if pid in rss and pid not in tree:
                tree.add(pid)
                pending.extend(children.get(pid, ()))
        if not tree:
            continue  # the pane's process ended meanwhile
        kb = sum(rss[pid] for pid in tree)
        for name in names:
            total, count = sessions.get(name, (0, 0))
            sessions[name] = (total + kb, count + len(tree))
    return sessions, rss.get(os.getpid(), 0), sum(rss.values())
