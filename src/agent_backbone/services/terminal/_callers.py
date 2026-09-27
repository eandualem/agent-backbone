"""Which tmux panes a local caller runs in.

The API asks this about a request's connection: the process on the other end
of a loopback connection (``lsof``), then the nearest ancestor of that process
that is a pane's first process (``ps``, ``tmux list-panes``). Nothing the
caller sends is used. Any step that can't be completed raises
``CallerUnknown``, so a caller that can't be placed is refused, never guessed.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress

from agent_backbone.services.terminal._core import _run_tmux

_TIMEOUT = 5.0


class CallerUnknown(Exception):
    """The caller could not be placed in a pane; the message says why."""


def is_loopback(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_loopback


async def _output(*argv: str) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
    except OSError as exc:
        raise CallerUnknown(f"{argv[0]} can't run here ({exc.strerror})") from exc
    try:
        async with asyncio.timeout(_TIMEOUT):
            out, _ = await proc.communicate()
    except TimeoutError as exc:
        raise CallerUnknown(f"{argv[0]} did not answer within {_TIMEOUT:g}s") from exc
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.communicate()
    return proc.returncode, out.decode(errors="replace")


def _endpoint(text: str) -> tuple[str, int] | None:
    host, _, port = text.rpartition(":")
    return (host, int(port)) if port.isdigit() else None


async def peer_pids(client_port: int, server_port: int) -> set[int]:
    """The processes holding the client end of a loopback connection."""
    code, out = await _output("lsof", "-nP", "-w", f"-iTCP:{client_port}", "-Fpn")
    if code not in (0, 1):  # 1: nothing matched
        raise CallerUnknown(f"lsof failed (exit {code})")
    pids: set[int] = set()
    pid = None
    for line in out.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None and "->" in line:
            local, remote = (_endpoint(part) for part in line[1:].split("->", 1))
            if (
                local is not None
                and remote is not None
                and local[1] == client_port
                and remote[1] == server_port
                and is_loopback(local[0])
                and is_loopback(remote[0])
            ):
                pids.add(pid)
    return pids


async def _parents() -> dict[int, int]:
    code, out = await _output("ps", "-A", "-o", "pid=,ppid=")
    if code != 0:
        raise CallerUnknown(f"ps failed (exit {code})")
    parents: dict[int, int] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            parents[int(parts[0])] = int(parts[1])
    return parents


async def _panes() -> dict[int, set[str]]:
    """Each pane's first process and the sessions showing it (a grouped
    viewer session shows the same pane under its own name)."""
    code, out, _ = await _run_tmux(
        "list-panes", "-a", "-F", "#{pane_pid}\t#{session_name}", capture_stdout=True
    )
    if code != 0:
        raise CallerUnknown("no tmux server answered")
    panes: dict[int, set[str]] = {}
    for line in out.decode(errors="replace").splitlines():
        pid, _, session = line.partition("\t")
        if pid.isdigit() and session:
            panes.setdefault(int(pid), set()).add(session)
    return panes


async def caller_sessions(client: tuple[str, int], server: tuple[str, int]) -> list[set[str]]:
    """For each process holding the client end of this connection, the
    sessions showing the pane it runs in."""
    if not is_loopback(client[0]) or not is_loopback(server[0]):
        raise CallerUnknown("the connection is not from this machine")
    pids = await peer_pids(client[1], server[1])
    if not pids:
        raise CallerUnknown("no local process holds the connection")
    parents, panes = await _parents(), await _panes()
    found = []
    for pid in sorted(pids):
        seen: set[int] = set()
        while pid not in panes:
            if pid in seen or pid <= 1 or pid not in parents:
                raise CallerUnknown("the calling process doesn't run in a tmux pane")
            seen.add(pid)
            pid = parents[pid]
        found.append(panes[pid])
    return found
