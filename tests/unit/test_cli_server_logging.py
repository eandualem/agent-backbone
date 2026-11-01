"""The backbone's log: every line timestamped in UTC, the file rotated by size."""

from __future__ import annotations

import logging
import logging.config
import re
from pathlib import Path

import pytest

from agent_backbone.base import log_policy, set_log_mode
from agent_backbone.cli.server import log_config
from agent_backbone.config import AgentsConfig, AgentSpec
from tests.conftest import make_config

_LINE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z (INFO|WARNING) [\w.]+: ")


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    mode, verbose = log_policy.mode, log_policy.verbose
    log_policy.verbose = False
    yield
    for handler in root.handlers:
        handler.close()
    root.handlers[:] = handlers
    root.setLevel(level)
    log_policy.mode, log_policy.verbose = mode, verbose


def test_uvicorn_and_backbone_lines_carry_utc_timestamps(tmp_path, restore_logging):
    log = tmp_path / "backbone.log"
    logging.config.dictConfig(
        log_config(make_config(tmp_path, settings={"diagnostics.mode": "debug"}), log)
    )
    access = logging.getLogger("uvicorn.access")
    access.setLevel(logging.INFO)  # uvicorn sets it from log_level
    access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5", "GET", "/api/agents", "1.1", 200)
    logging.getLogger("agent_backbone.services.jobs").warning("Job %s failed", "prune")
    first, second = log.read_text().splitlines()
    assert _LINE.match(first) and first.endswith(
        'uvicorn.access: 127.0.0.1:5 - "GET /api/agents HTTP/1.1" 200'
    )
    assert _LINE.match(second) and second.endswith("Job prune failed")


def test_the_log_file_rotates_at_its_size_keeping_its_backups(tmp_path, restore_logging):
    log = tmp_path / "backbone.log"
    config = make_config(tmp_path, settings={"backbone.log_max_mb": 1, "backbone.log_backups": 2})
    logging.config.dictConfig(log_config(config, log))
    for _ in range(4000):
        logging.getLogger("agent_backbone").warning("x" * 1000)
    assert sorted(path.name for path in tmp_path.glob("backbone.log*")) == [
        "backbone.log",
        "backbone.log.1",
        "backbone.log.2",
    ]
    assert log.stat().st_size <= 1024 * 1024


def test_without_a_log_file_lines_go_to_stderr(tmp_path, restore_logging, capsys):
    logging.config.dictConfig(log_config(make_config(tmp_path), None))
    logging.getLogger("agent_backbone").warning("restarting onto the new code")
    assert _LINE.match(capsys.readouterr().err)


async def test_a_detached_backbone_keeps_its_log_file(tmp_path):
    from unittest.mock import AsyncMock, patch

    from agent_backbone.cli.server import _up_detached

    with (
        patch("agent_backbone.services.terminal.session_exists", AsyncMock(return_value=False)),
        patch(
            "agent_backbone.services.terminal.start_session", AsyncMock(return_value=True)
        ) as start,
        patch("agent_backbone.cli.service.state", return_value="installed"),
    ):
        assert await _up_detached(make_config(tmp_path), tmp_path / "backbone.log") == 0
    command = start.call_args.kwargs["command"]
    assert command[-3:] == ["up", "--log-file", str(tmp_path / "backbone.log")]


def test_a_quoted_home_path_is_expanded_before_logging_starts(tmp_path, monkeypatch):
    import argparse
    from unittest.mock import AsyncMock, patch

    from agent_backbone.cli import server

    monkeypatch.setenv("HOME", str(tmp_path))
    args = argparse.Namespace(detach=False, reload=False, log_file=Path("~/backbone.log"))
    with (
        patch.object(server._common, "load_config", AsyncMock(return_value=make_config(tmp_path))),
        patch.object(server, "_run_server") as run,
    ):
        assert server.cmd_up(args) == 0
    assert run.call_args.kwargs["log_file"] == (tmp_path / "backbone.log").resolve()


def test_a_log_keeps_at_least_one_backup():
    from agent_backbone.config import validate_setting

    with pytest.raises(ValueError, match="positive"):
        validate_setting("backbone.log_backups", 0)


@pytest.mark.parametrize("value", [-1, 101])
def test_a_pressure_threshold_is_a_percentage(value):
    from agent_backbone.config import validate_setting

    with pytest.raises(ValueError, match="percentage"):
        validate_setting("diagnostics.pressure_free_percent", value)


def test_reload_and_a_log_file_are_refused_together(tmp_path, capsys):
    import argparse
    from unittest.mock import AsyncMock, patch

    from agent_backbone.cli import server

    args = argparse.Namespace(detach=False, reload=True, log_file=tmp_path / "backbone.log")
    with (
        patch.object(server._common, "load_config", AsyncMock(return_value=make_config(tmp_path))),
        patch.object(server, "_run_server") as run,
    ):
        assert server.cmd_up(args) == 2
    run.assert_not_called()
    assert "--reload" in capsys.readouterr().out


def test_a_negative_log_size_is_refused_and_zero_is_kept():
    from agent_backbone.config import validate_setting

    with pytest.raises(ValueError, match="zero or a positive"):
        validate_setting("backbone.log_max_mb", -1)
    assert validate_setting("backbone.log_max_mb", 0) == 0


def test_reload_factory_uses_the_actual_listener_endpoint(tmp_path, monkeypatch):
    import os
    from dataclasses import replace
    from unittest.mock import patch

    from agent_backbone.cli.server import _reload_app, _run_server
    from agent_backbone.config import bootstrap_config

    monkeypatch.setenv("BACKBONE_PORT", "7120")
    monkeypatch.delenv("BACKBONE_HOST", raising=False)
    config = bootstrap_config(tmp_path)
    config = replace(config, backbone=replace(config.backbone, host="localhost", port=8234))

    def reload_child(*args, **kwargs):
        with patch("agent_backbone.api.app.create_app", side_effect=lambda config: config):
            boot = _reload_app()
        assert (boot.backbone.host, boot.backbone.port) == (kwargs["host"], kwargs["port"])
        assert boot.backbone.port == 8234

    with patch("uvicorn.run", side_effect=reload_child):
        _run_server(config, reload=True)
    assert os.environ["BACKBONE_PORT"] == "7120"
    assert "BACKBONE_HOST" not in os.environ


@pytest.mark.parametrize(
    "runtime", ["claude", "codex", "opencode", "gemini", "deepcode", "aider", "shell"]
)
def test_modes_control_actual_emission_and_keep_developer_detail(
    tmp_path, restore_logging, runtime
):
    path = tmp_path / "controlled.log"
    config = make_config(
        tmp_path,
        settings={"diagnostics.mode": "off"},
        agents=AgentsConfig(
            specs={"worker": AgentSpec(name="worker", dir=str(tmp_path), runtime=runtime)}
        ),
    )
    logging.config.dictConfig(log_config(config, path))
    own = logging.getLogger("agent_backbone.example")
    access = logging.getLogger("uvicorn.access")
    access.setLevel(logging.DEBUG)
    log_policy.verbose = True
    own.error("off error")
    access.info('%s - "%s %s HTTP/%s" %d', "local", "GET", "/health", "1.1", 200)
    assert not path.exists()  # Off does not even create the optional log.

    log_policy.verbose = False
    set_log_mode("standard")
    own.debug("developer-only detail")
    access.info('%s - "%s %s HTTP/%s" %d', "local", "GET", "/health", "1.1", 200)
    try:
        raise ValueError("causal developer detail")
    except ValueError:
        own.exception("Operation failed")
    ordinary = path.read_text()
    assert "Operation failed (ValueError)" in ordinary
    assert "developer" not in ordinary and "Traceback" not in ordinary and "GET" not in ordinary

    set_log_mode("debug")
    own.debug("hook stale after 600s; terminal failure overrides busy")
    try:
        raise ValueError("retry after 30s")
    except ValueError:
        own.exception("Provider failed")
    access.info(
        '%s - "%s %s HTTP/%s" %d', "local", "GET", "/health?token=EXAMPLE_VALUE", "1.1", 200
    )
    logging.getLogger("httpx").debug("Authorization: EXAMPLE_VALUE")
    detailed = path.read_text()
    assert "hook stale after 600s; terminal failure overrides busy" in detailed
    assert "ValueError: retry after 30s" in detailed and "Traceback" in detailed
    assert "GET /health HTTP/1.1" in detailed and "EXAMPLE_VALUE" not in detailed
    set_log_mode("off")
    own.error("off again")
    assert path.read_text() == detailed


async def test_scoped_cli_reads_logging_policy_through_api(tmp_path):
    from dataclasses import replace
    from unittest.mock import AsyncMock, patch

    from agent_backbone.cli import _common
    from agent_backbone.config import agent_api_key

    config = replace(make_config(tmp_path), api_key=agent_api_key("example-key", "a" * 32))
    with (
        patch.object(_common, "read_client_config", AsyncMock(return_value=config)),
        patch.object(_common, "api", AsyncMock(return_value=(200, {"value": "off"}))) as api,
    ):
        assert await _common.read_log_mode() == "off"
        assert api.call_args.args[2] == "/api/config/diagnostics.mode"
        api.return_value = None
        assert await _common.read_log_mode() == "off"


def test_reload_logging_configuration_survives_spawn_serialization(tmp_path, restore_logging):
    import pickle

    config = log_config(make_config(tmp_path, settings={"diagnostics.mode": "off"}), None)
    restored = pickle.loads(pickle.dumps(config))
    logging.config.dictConfig(restored)
    handler = logging.getLogger().handlers[0]
    assert handler.filters == [log_policy]
    assert not handler.filters[0].filter(logging.makeLogRecord({"levelno": logging.ERROR}))
    set_log_mode("standard")
    assert handler.filters[0].filter(
        logging.makeLogRecord({"levelno": logging.ERROR, "name": "agent_backbone"})
    )


@pytest.mark.parametrize("exit_code", [1, 3])
def test_off_keeps_explicit_server_failure_result_without_log_capture(
    tmp_path, restore_logging, capsys, exit_code
):
    import argparse
    from unittest.mock import AsyncMock, patch

    from agent_backbone.cli import server

    path = tmp_path / "optional.log"
    config = make_config(tmp_path, settings={"diagnostics.mode": "off"})
    args = argparse.Namespace(detach=False, reload=False, log_file=path)
    with (
        patch.object(server._common, "load_config", AsyncMock(return_value=config)),
        patch("uvicorn.Server.run", side_effect=SystemExit(exit_code)),
    ):
        with pytest.raises(SystemExit) as result:
            server.cmd_up(args)
    assert result.value.code == exit_code
    assert f"exit {exit_code}" in capsys.readouterr().err
    assert not path.exists()


def test_reload_child_restores_explicit_verbose_policy(tmp_path, restore_logging):
    import pickle

    log_policy.verbose = True
    wire = pickle.dumps(log_config(make_config(tmp_path), None))
    # A spawned interpreter starts with the module defaults.
    set_log_mode("off")
    log_policy.verbose = False
    logging.config.dictConfig(pickle.loads(wire))
    record = logging.makeLogRecord({"name": "agent_backbone", "levelno": logging.DEBUG})
    assert log_policy.mode == "standard" and log_policy.verbose
    assert log_policy.filter(record)
    set_log_mode("off")
    assert not log_policy.filter(record)


@pytest.mark.parametrize("mode,verbose", [("debug", False), ("standard", True), ("off", True)])
@pytest.mark.parametrize("protocol_level", ["info", "debug"])
async def test_websocket_protocol_does_not_log_credentials_or_wire_frames(
    tmp_path, restore_logging, mode, verbose, protocol_level
):
    import io

    from uvicorn import Config
    from uvicorn.protocols.websockets.websockets_impl import WebSocketProtocol
    from uvicorn.server import ServerState
    from websockets.frames import Frame, Opcode

    async def app(scope, receive, send):
        pass

    log_policy.verbose = verbose
    output = io.StringIO()
    settings = log_config(make_config(tmp_path, settings={"diagnostics.mode": mode}), None)
    settings["handlers"]["default"]["stream"] = output
    protocol = WebSocketProtocol(
        Config(app, log_config=settings, log_level=protocol_level), ServerState(), {}
    )
    protocol.reader.feed_data(
        b"GET /socket.io/?token=QUERY_EXAMPLE HTTP/1.1\r\n"
        b"Host: localhost\r\nAuthorization: Bearer HEADER_EXAMPLE\r\n\r\n"
    )
    await protocol.read_http_request()
    protocol.reader.feed_data(
        Frame(Opcode.TEXT, b'40/sessions,{"api_key":"FRAME_EXAMPLE"}').serialize(mask=True)
    )
    await protocol.read_frame(1024)
    protocol.scope = {
        "client": ("127.0.0.1", 1234),
        "root_path": "",
        "path": "/socket.io/",
        "query_string": b"token=QUERY_EXAMPLE",
    }
    await protocol.asgi_send({"type": "websocket.accept"})
    logging.getLogger("agent_backbone.example").debug("hook stale after 600s")
    logging.getLogger("uvicorn.error").error("ASGI application failed")
    captured = output.getvalue()
    assert all(
        value not in captured for value in ("QUERY_EXAMPLE", "HEADER_EXAMPLE", "FRAME_EXAMPLE")
    )
    if mode == "off":
        assert not captured
    else:
        assert "hook stale after 600s" in captured
        assert "ASGI application failed" in captured
        assert "WebSocket /socket.io/" in captured
