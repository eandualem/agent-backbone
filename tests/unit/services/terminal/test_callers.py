"""Placing a local caller in a tmux pane: lsof, then process ancestry, then panes."""

from __future__ import annotations

import asyncio
import os
import shutil
import socket

import pytest

from agent_backbone.services.terminal import CallerUnknown, _callers, caller_sessions

_REAL_EXEC = asyncio.create_subprocess_exec
"""Captured before the suite's guard patches it: these two tests run real tools, never tmux."""

CLIENT, SERVER = ("127.0.0.1", 53166), ("127.0.0.1", 8420)
LSOF = (
    "p900\nf4\nn127.0.0.1:53166->127.0.0.1:8420\n"  # the caller's end
    "p77\nf9\nn127.0.0.1:8420->127.0.0.1:53166\n"  # the server's end
)
# Process trees as measured live: the pane's first process is the runtime.
TREES = {
    "claude": {900: 810, 810: 500, 500: 1},  # backbone <- zsh <- claude (pane) <- tmux
    "codex": {900: 820, 820: 815, 815: 500, 500: 1},  # <- shell <- helper <- codex (pane)
    "opencode": {900: 500, 500: 1},  # backbone <- opencode (pane), from its bash tool
    "shell": {900: 500, 500: 1},  # a command typed at the pane's own shell
}


def _fake(monkeypatch, *, lsof=LSOF, parents=None, panes=None, lsof_code=0):
    parents = TREES["claude"] if parents is None else parents
    panes = {500: {"ike"}} if panes is None else panes

    async def output(*argv):
        if argv[0] == "lsof":
            return lsof_code, lsof
        return 0, "".join(f"{pid:>6} {ppid:>6}\n" for pid, ppid in parents.items())

    async def tmux(*args, capture_stdout=False):
        lines = [f"{pid}\t{name}" for pid, names in panes.items() for name in sorted(names)]
        return 0, "\n".join(lines).encode(), b""

    monkeypatch.setattr(_callers, "_output", output)
    monkeypatch.setattr(_callers, "_run_tmux", tmux)


@pytest.mark.parametrize("runtime", sorted(TREES))
async def test_a_command_run_by_the_runtime_reaches_its_agents_pane(monkeypatch, runtime):
    _fake(monkeypatch, parents=TREES[runtime])
    assert await caller_sessions(CLIENT, SERVER) == [{"ike"}]


async def test_a_grouped_viewer_session_shows_the_same_pane(monkeypatch):
    _fake(monkeypatch, panes={500: {"ike", "ike-view"}})
    assert await caller_sessions(CLIENT, SERVER) == [{"ike", "ike-view"}]


async def test_only_the_callers_end_of_the_connection_counts(monkeypatch):
    # The server's own socket names the same port; it is not the caller.
    _fake(monkeypatch, parents={900: 500, 500: 1, 77: 1})
    assert await caller_sessions(CLIENT, SERVER) == [{"ike"}]


async def test_a_connection_on_other_loopback_addresses_is_not_the_callers(monkeypatch):
    # Same ports, different loopback addresses: another process's connection.
    lsof = LSOF + "p901\nf4\nn127.0.0.2:53166->127.0.0.1:8420\n"
    _fake(monkeypatch, lsof=lsof, parents={900: 500, 901: 600, 500: 1, 600: 1},
          panes={500: {"ike"}, 600: {"una"}})  # fmt: skip
    assert await caller_sessions(CLIENT, SERVER) == [{"ike"}]


async def test_ipv4_mapped_and_ipv6_loopback_are_local(monkeypatch):
    lsof = "p900\nf4\nn[::1]:53166->[::ffff:127.0.0.1]:8420\n"
    _fake(monkeypatch, lsof=lsof)
    assert await caller_sessions(("::1", 53166), ("::ffff:127.0.0.1", 8420)) == [{"ike"}]


@pytest.mark.parametrize(
    ("setup", "why"),
    [
        ({"client": ("192.0.2.7", 53166)}, "not from this machine"),
        ({"lsof": ""}, "no local process"),
        ({"lsof_code": 2}, "lsof failed"),
        ({"parents": {900: 1}}, "doesn't run in a tmux pane"),  # reparented: a daemon
        ({"parents": {900: 901, 901: 900}}, "doesn't run in a tmux pane"),  # a loop
        ({"parents": {}}, "doesn't run in a tmux pane"),  # gone before ps ran
    ],
)
async def test_a_caller_that_cant_be_placed_is_refused(monkeypatch, setup, why):
    client = setup.pop("client", CLIENT)
    _fake(monkeypatch, **setup)
    with pytest.raises(CallerUnknown, match=why):
        await caller_sessions(client, SERVER)


async def test_every_process_holding_the_connection_is_placed(monkeypatch):
    lsof = LSOF + "p901\nf4\nn127.0.0.1:53166->127.0.0.1:8420\n"
    _fake(monkeypatch, lsof=lsof, parents={900: 500, 901: 600, 500: 1, 600: 1},
          panes={500: {"ike"}, 600: {"una"}})  # fmt: skip
    assert await caller_sessions(CLIENT, SERVER) == [{"ike"}, {"una"}]


async def test_a_missing_tool_is_refused_not_guessed(monkeypatch):
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _REAL_EXEC)
    monkeypatch.setenv("PATH", "")
    with pytest.raises(CallerUnknown, match="can't run here"):
        await caller_sessions(CLIENT, SERVER)


@pytest.mark.skipif(shutil.which("lsof") is None, reason="lsof is not installed")
async def test_the_real_lsof_finds_this_process_on_its_own_connection(monkeypatch):
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _REAL_EXEC)
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    client = socket.create_connection(server.getsockname())
    accepted, _ = server.accept()
    try:
        pids = await _callers.peer_pids(client.getsockname(), server.getsockname())
        assert pids == {os.getpid()}
        assert (await _callers._parents())[os.getpid()] == os.getppid()
    finally:
        for sock in (accepted, client, server):
            sock.close()
