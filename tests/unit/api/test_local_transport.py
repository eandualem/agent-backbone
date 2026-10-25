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
    with local.private_listener(root, str(path)):
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
    with local.private_listener(root, str(path)):
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
