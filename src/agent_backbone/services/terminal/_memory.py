"""Resident memory of each tmux session's processes (``ps``, ``tmux list-panes``)."""

from __future__ import annotations

import os

from agent_backbone.services.terminal._callers import CallerUnknown, _output
from agent_backbone.services.terminal._core import _run_tmux

_NO_SERVER = ("no server running", "No such file or directory")


async def _panes() -> dict[int, set[str]] | None:
    """Each pane's first process and its sessions; empty when no tmux server
    runs, None when tmux could not be read."""
    code, out, err = await _run_tmux(
        "list-panes", "-a", "-F", "#{pane_pid}\t#{session_name}", capture_stdout=True
    )
    if code != 0:
        message = err.decode(errors="replace")
        return {} if any(reason in message for reason in _NO_SERVER) else None
    panes: dict[int, set[str]] = {}
    for line in out.decode(errors="replace").splitlines():
        pid, _, session = line.partition("\t")
        if pid.isdigit() and session:
            panes.setdefault(int(pid), set()).add(session)
    return panes


async def process_memory() -> tuple[dict[str, tuple[int, int]] | None, int, int]:
    """Resident memory in KB: per session, the summed process trees of its
    panes with their process count (None when tmux could not be read); this
    process; and every process.

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
    own, total = rss.get(os.getpid(), 0), sum(rss.values())
    panes = await _panes()
    if panes is None:
        return None, own, total
    sessions: dict[str, tuple[int, int]] = {}
    for pane, names in panes.items():
        tree, pending = set(), [pane]
        while pending:
            pid = pending.pop()
            if pid in rss and pid not in tree:
                tree.add(pid)
                pending.extend(children.get(pid, ()))
        tree.discard(os.getpid())  # counted on its own, even when it runs in a pane
        if not tree:
            continue  # the pane's process ended meanwhile
        kb = sum(rss[pid] for pid in tree)
        for name in names:
            total_kb, count = sessions.get(name, (0, 0))
            sessions[name] = (total_kb + kb, count + len(tree))
    return sessions, own, total
