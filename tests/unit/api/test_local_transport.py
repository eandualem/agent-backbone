"""Real local sockets, without a running Backbone service or model.

This API boundary is runtime-independent: claude, codex, opencode, gemini,
deepcode, aider and shell use the same listener and kernel peer check.
"""

import asyncio
import os
import socket
import tempfile
from pathlib import Path

import pytest
import uvicorn

from agent_backbone.api import local_transport as local


@pytest.fixture
def root():
    # Keep below the platform's Unix socket path-length limit.
    with tempfile.TemporaryDirectory(prefix="bb-uds-", dir="/tmp") as folder:
        yield Path(folder)


def test_listener_is_private_and_removed_on_close(root):
    path = root / "api.sock"
    with local.private_listener(root, str(path)) as listener:
        assert not listener.get_inheritable()
        assert path.stat().st_mode & 0o777 == 0o600
        # Another startup cannot mistake a bound, not-yet-served socket for stale.
        with pytest.raises(ValueError, match="already in use"):
            with local.private_listener(root, str(path)):
                pytest.fail("startup listener was replaced")
    assert not path.exists()


@pytest.mark.parametrize("unsafe", ["parent", "file", "outside", "symlink"])
def test_unsafe_paths_are_not_replaced(root, unsafe):
    path = root / "api.sock"
    if unsafe == "parent":
        root.chmod(0o755)
    elif unsafe == "file":
        path.write_text("preserve")
    elif unsafe == "outside":
        path = root.parent / "outside.sock"
    else:
        target = root / "target"
        target.write_text("preserve")
        path.symlink_to(target)
    with pytest.raises(ValueError):
        with local.private_listener(root, str(path)):
            pytest.fail("unsafe listener was opened")
    if unsafe in {"file", "symlink"}:
        assert path.read_text() == "preserve"


def test_live_socket_is_preserved_but_an_owned_stale_socket_is_replaced(root):
    path = root / "api.sock"
    with socket.socket(socket.AF_UNIX) as existing:
        existing.bind(str(path))
        path.chmod(0o600)
        existing.listen(1)
        with pytest.raises(ValueError, match="already in use"):
            with local.private_listener(root, str(path)):
                pytest.fail("live listener was replaced")
    with local.private_listener(root, str(path)):
        assert path.exists()


def test_kernel_peer_identity_matches_this_account():
    first, second = socket.socketpair()
    try:
        assert local.peer_uid(first) == os.geteuid()
        assert local.peer_uid(second) == os.geteuid()
        assert local.peer_pid(first) == os.getpid()
        assert local.peer_pid(second) == os.getpid()
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("allowed", [True, False])
async def test_peer_checked_before_http_application_receives_request(root, monkeypatch, allowed):
    seen = []

    async def application(scope, receive, send):
        seen.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    if not allowed:
        monkeypatch.setattr(local, "peer_uid", lambda connection: os.geteuid() + 1)
    config = uvicorn.Config(
        application, http=local.LocalPeerProtocol, lifespan="off", log_config=None
    )
    config.load()
    state = uvicorn.Server(config).server_state
    path = root / "api.sock"
    with local.private_listener(root, str(path)) as listener:
        server = await asyncio.get_running_loop().create_server(
            lambda: local.LocalPeerProtocol(config=config, server_state=state, app_state={}),
            sock=listener,
        )
        try:
            reader, writer = await asyncio.open_unix_connection(str(path))
            try:
                writer.write(
                    b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
                )
                try:
                    response = await asyncio.wait_for(reader.read(), 2)
                except ConnectionResetError:
                    response = b""
                assert seen == (["/health"] if allowed else [])
                assert (b"200 OK" in response) is allowed
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionResetError:
                    pass
        finally:
            server.close()
            await server.wait_closed()


def test_configured_server_passes_private_listener_and_existing_tcp_socket(root):
    from types import SimpleNamespace
    from unittest.mock import patch

    from agent_backbone.cli.server import _run_server
    from agent_backbone.config import AgentsConfig, build_config

    path = root / "api.sock"
    config = build_config(
        root, settings={"backbone.socket_path": str(path)}, agents=AgentsConfig(), env={}
    )
    app = SimpleNamespace(state=SimpleNamespace())
    with (
        patch("agent_backbone.api.app.create_app", return_value=app),
        patch("uvicorn.Server") as server,
    ):
        tcp = server.return_value.config.bind_socket.return_value

        def run(*, sockets):
            assert sockets[0] is tcp
            tcp.set_inheritable.assert_called_once_with(False)
            assert not sockets[1].get_inheritable()
            assert sockets[1].family == socket.AF_UNIX
            assert path.stat().st_mode & 0o777 == 0o600

        server.return_value.run.side_effect = run
        _run_server(config)
        assert server.call_args.args[0].http is local.LocalPeerProtocol
        tcp.close.assert_called_once()
    assert not path.exists()
    with pytest.raises(ValueError, match="without --reload"):
        _run_server(config, reload=True)


def test_inconclusive_probe_is_bounded_and_preserves_socket(root):
    from unittest.mock import MagicMock, patch

    path = root / "api.sock"
    with socket.socket(socket.AF_UNIX) as existing:
        existing.bind(str(path))
        path.chmod(0o600)
        existing.listen(1)
        probe = MagicMock()
        probe.__enter__.return_value = probe
        probe.connect.side_effect = TimeoutError("accept queue saturated")
        with patch.object(local.socket, "socket", return_value=probe):
            with pytest.raises(TimeoutError):
                with local.private_listener(root, str(path)):
                    pytest.fail("inconclusive socket was replaced")
        probe.settimeout.assert_called_once_with(1.0)
        assert path.exists()


def test_linux_peer_uid_preserves_unsigned_range(monkeypatch):
    import struct
    from unittest.mock import Mock

    monkeypatch.setattr(local.sys, "platform", "linux")
    monkeypatch.setattr(local.socket, "SO_PEERCRED", 17, raising=False)
    connection = Mock()
    connection.getsockopt.return_value = struct.pack("iII", 123, 3_000_000_000, 3_000_000_001)
    assert local.peer_uid(connection) == 3_000_000_000


async def test_unix_validation_uses_kernel_pid_and_preserves_agent_identity(
    root, api_app, auth_headers, monkeypatch
):
    from unittest.mock import AsyncMock

    import httpx

    from agent_backbone.services.terminal import _callers

    agent = next(iter(api_app.state.config.agents)).name
    monkeypatch.setattr(_callers, "_parents", AsyncMock(return_value={os.getpid(): 100}))
    monkeypatch.setattr(_callers, "_panes", AsyncMock(return_value={100: {agent}}))
    validate = AsyncMock(
        return_value=(
            "closed",
            {
                "receipt": {},
                "grant": {"status": "closed", "claimed_at": 1, "expires_at": 2, "closed_at": 2},
            },
        )
    )
    monkeypatch.setattr(api_app.state.db.signing, "validate", validate)
    config = uvicorn.Config(api_app, http=local.LocalPeerProtocol, lifespan="off", log_config=None)
    config.load()
    state = uvicorn.Server(config).server_state
    path = root / "validate.sock"
    with local.private_listener(root, str(path)) as listener:
        server = await asyncio.get_running_loop().create_server(
            lambda: local.LocalPeerProtocol(config=config, server_state=state, app_state={}),
            sock=listener,
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=str(path)), base_url="http://localhost"
            ) as client:
                response = await client.post(
                    "/api/messages/validate",
                    headers=auth_headers,
                    json={"confirmation_id": "12345678-1234-4234-8234-123456789abc", "done": True},
                )
            assert response.status_code == 200, response.text
            assert response.json()["agent"] == agent
            assert validate.await_args.kwargs["agent"] == agent
            assert not api_app.state.validations._incidents
        finally:
            server.close()
            await server.wait_closed()


async def test_concurrent_starts_cannot_replace_the_live_socket(root):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Event

    path = root / "race.sock"
    with socket.socket(socket.AF_UNIX) as stale:
        stale.bind(str(path))
        path.chmod(0o600)
    start, attempted = Barrier(2), Barrier(2)
    release = Event()

    def launch():
        start.wait(timeout=3)
        try:
            with local.private_listener(root, str(path)):
                attempted.wait(timeout=3)
                release.wait(timeout=3)
                return "listening"
        except ValueError as exc:
            assert "already in use" in str(exc)
            attempted.wait(timeout=3)
            with socket.socket(socket.AF_UNIX) as probe:
                probe.connect(str(path))
            release.set()
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(launch), pool.submit(launch)
        assert sorted([first.result(timeout=5), second.result(timeout=5)]) == [
            "listening",
            "refused",
        ]
    assert not path.exists()


async def test_real_uvicorn_serves_tcp_and_unix_and_cleans_up(root, api_app):
    import httpx

    path = root / "both.sock"
    config = uvicorn.Config(
        api_app,
        host="127.0.0.1",
        port=0,
        http=local.LocalPeerProtocol,
        lifespan="off",
        log_config=None,
    )
    server = uvicorn.Server(config)
    tcp = config.bind_socket()
    port = tcp.getsockname()[1]
    try:
        with local.private_listener(root, str(path)) as unix:
            task = asyncio.create_task(server.serve(sockets=[tcp, unix]))
            try:
                async with asyncio.timeout(3):
                    while not server.started:
                        if task.done():
                            await task
                            pytest.fail("server exited before startup")
                        await asyncio.sleep(0.01)
                async with (
                    httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as network,
                    httpx.AsyncClient(
                        transport=httpx.AsyncHTTPTransport(uds=str(path)),
                        base_url="http://localhost",
                    ) as private,
                ):
                    responses = await asyncio.gather(network.get("/health"), private.get("/health"))
                assert [response.status_code for response in responses] == [200, 200]
                assert responses[0].json() == responses[1].json()
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, 3)
            assert all(not listener.is_serving() for listener in server.servers)
    finally:
        tcp.close()
    assert not path.exists()
    assert tcp.fileno() == -1 and unix.fileno() == -1


@pytest.mark.parametrize("event_loop", ["asyncio", "uvloop"])
def test_runtime_child_cannot_retain_listeners_across_upgrade(root, monkeypatch, event_loop):
    import json
    import sys
    from types import SimpleNamespace

    from agent_backbone.cli.server import _run_server
    from agent_backbone.config import AgentsConfig, build_config

    runner = asyncio.run if event_loop == "asyncio" else pytest.importorskip("uvloop").run
    config = build_config(
        root,
        settings={"backbone.socket_path": str(root / "api.sock"), "backbone.port": 0},
        agents=AgentsConfig(),
        env={},
    )
    app = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr("agent_backbone.api.app.create_app", lambda config: app)

    async def child_probe(sockets):
        # Force uvloop's fork path on macOS too: its posix_spawn path closes
        # inherited descriptors, concealing the Linux subprocess behavior.
        child = await asyncio.subprocess.create_subprocess_exec(
            sys.executable,
            "-I",
            "-c",
            "import os, sys, json\n"
            "retained = []\n"
            "for fd in map(int, sys.argv[1:]):\n"
            " try: os.fstat(fd); retained.append(fd)\n"
            " except OSError: pass\n"
            "print(json.dumps(retained), flush=True)\n"
            "sys.stdin.read()\n",
            *(str(listener.fileno()) for listener in sockets),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            preexec_fn=os.getpid,
        )
        try:
            retained = json.loads(await asyncio.wait_for(child.stdout.readline(), 5))
            assert retained == []
            endpoint = sockets[0].getsockname()
            for listener in sockets:
                listener.close()
            # A still-running child must not prevent the replacement daemon
            # from binding the same TCP endpoint after graceful shutdown.
            with socket.socket() as replacement:
                replacement.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                replacement.bind(endpoint)
                replacement.listen()
            assert child.returncode is None
        finally:
            child.terminate()
            await asyncio.wait_for(child.communicate(), 5)

    def run(server, *, sockets):
        runner(child_probe(sockets))
        server.started = True

    monkeypatch.setattr(uvicorn.Server, "run", run)
    _run_server(config)
    assert not (root / "api.sock").exists()
