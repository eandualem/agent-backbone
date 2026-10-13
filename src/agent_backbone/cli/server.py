"""``backbone up|down|status|config`` — running and inspecting the backbone."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

from agent_backbone.cli import _common
from agent_backbone.cli.presentation import note, print_table
from agent_backbone.config import (
    SETTINGS_DEFAULTS,
    SETTINGS_HELP,
    BackboneConfig,
    validate_setting,
)

log = logging.getLogger(__name__)


class UTCFormatter(logging.Formatter):
    """``2026-10-13T12:50:01.123Z``: every log line can be placed in time."""

    converter = time.gmtime
    default_time_format = "%Y-%m-%dT%H:%M:%S"
    default_msec_format = "%s.%03dZ"


def log_config(config: BackboneConfig, log_file: Path | None) -> dict:
    """One timestamped format for the backbone and uvicorn (its access lines too),
    written to ``log_file`` with size-based rotation, else to stderr."""
    if log_file is None:
        handler: dict = {"class": "logging.StreamHandler", "stream": "ext://sys.stderr"}
    else:
        handler = {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(log_file),
            "maxBytes": max(0, config.settings.get("backbone.log_max_mb", 20)) * 1024 * 1024,
            "backupCount": max(1, config.settings.get("backbone.log_backups", 3)),
            "encoding": "utf-8",
        }
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "utc": {
                "()": UTCFormatter,
                "format": "%(asctime)s %(levelname)s %(name)s: %(message)s",
            }
        },
        "handlers": {"default": {**handler, "formatter": "utc"}},
        # The root keeps the level `main` chose (--verbose); uvicorn's loggers
        # get theirs from `log_level` and reach the same handler through it.
        "root": {"handlers": ["default"], "level": logging.getLogger().level},
        "loggers": {
            name: {"handlers": [], "propagate": True}
            for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
        },
    }


def _run_server(config: BackboneConfig, reload: bool = False, log_file: Path | None = None) -> None:
    import uvicorn

    logging_config = log_config(config, log_file)
    if reload:
        uvicorn.run(
            "agent_backbone.api.app:create_app",
            factory=True,
            host=config.backbone.host,
            port=config.backbone.port,
            reload=True,
            reload_dirs=["src"],
            log_level="info",
            log_config=logging_config,
        )
        return

    from agent_backbone.api.app import create_app

    app = create_app(config)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.backbone.host,
            port=config.backbone.port,
            log_level="info",
            log_config=logging_config,
            workers=1,
        )
    )
    inner = getattr(app, "other_asgi_app", app)
    # An internal upgrade must not send SIGTERM: Uvicorn replays captured
    # signals after shutdown, which would kill us before the exec below.
    inner.state.request_shutdown = lambda: setattr(server, "should_exit", True)
    try:
        server.run()
    except KeyboardInterrupt:
        return
    if not server.started:
        raise SystemExit(3)  # preserve uvicorn.run's startup-failure exit status
    if restart_requested(app):
        # The upgrade watch asked for new code: become a fresh `backbone up`
        # in place, so the login service or tmux session is unchanged.
        log.warning("restarting onto the new code")
        os.execv(sys.executable, [sys.executable, "-m", "agent_backbone.cli", *sys.argv[1:]])


def restart_requested(app) -> bool:
    """Whether the upgrade watch asked for a restart. ``create_app`` returns the
    Socket.IO wrapper; the flag is on the FastAPI app inside it."""
    inner = getattr(app, "other_asgi_app", app)
    state = getattr(inner, "state", None)
    return bool(getattr(state, "restart_requested", False))


async def _up_detached(config: BackboneConfig, log_file: Path | None = None) -> int:
    from agent_backbone.services.terminal import session_exists, start_session

    session = config.backbone.session_name
    if await session_exists(session):
        print(f"backbone already running in tmux session '{session}'")
        return 0
    command = [sys.executable, "-m", "agent_backbone.cli", "up"]
    if log_file is not None:
        command += ["--log-file", str(log_file)]
    env = {"BACKBONE_DATA_DIR": str(config.data_dir)}
    ok = await start_session(session, working_dir=str(Path.cwd()), command=command, environment=env)
    if ok:
        print(f"backbone started in tmux session '{session}' (attach: tmux attach -t {session})")
        from agent_backbone.cli.service import state as service_state

        if service_state() == "not installed":
            print("tip: `backbone service install` starts it at login and restarts it if it dies")
        return 0
    print("failed to start backbone session")
    return 1


def cmd_up(args: argparse.Namespace) -> int:
    config = asyncio.run(_common.load_config())
    # A quoted `~/…` reaches us unexpanded; a detached run starts elsewhere.
    log_file = args.log_file.expanduser().resolve() if args.log_file else None
    if log_file is not None and args.reload:
        # The reloader and its server would each rotate the same file.
        print("error: --log-file can't be combined with --reload")
        return 2
    if args.detach:
        return asyncio.run(_up_detached(config, log_file))
    _run_server(config, reload=args.reload, log_file=log_file)
    return 0


async def _down(config: BackboneConfig) -> int:
    from agent_backbone.services.terminal import graceful_close, session_exists

    session = config.backbone.session_name
    if not await session_exists(session):
        print("backbone is not running in tmux")
        return 0
    if not await graceful_close(session, timeout=15.0):
        print(f"failed to stop backbone session '{session}'")
        return 1
    print("backbone stopped")
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    return asyncio.run(_down(asyncio.run(_common.load_config())))


async def _config_cmd(args: argparse.Namespace) -> int:
    sub = args.config_command
    boot = await _common.read_client_config()

    if sub == "list":
        async with _common.Direct(boot) as direct:
            stored = await direct.db.settings.all()
        print_table(
            "Configuration",
            ("Setting", "Value", "Source"),
            [
                (
                    key,
                    json.dumps(stored.get(key, SETTINGS_DEFAULTS[key])),
                    "explicit" if key in stored else "default",
                )
                for key in sorted(SETTINGS_DEFAULTS)
            ],
        )
        note("Setting help: backbone config get KEY")
        return 0

    if sub == "get":
        async with _common.Direct(boot) as direct:
            stored = await direct.db.settings.all()
        if args.key not in SETTINGS_DEFAULTS:
            print(f"unknown setting '{args.key}'")
            return 1
        print(json.dumps(stored.get(args.key, SETTINGS_DEFAULTS[args.key])))
        if args.key in SETTINGS_HELP:
            print(f"  {SETTINGS_HELP[args.key]}")
        return 0

    if sub == "set":
        value = _common.parse_value(args.value)
        try:
            clean = validate_setting(args.key, value)
        except (KeyError, ValueError) as exc:
            print(f"error: {exc}")
            return 1
        if await _common.api_up(boot):
            result = await _common.api(
                boot, "PUT", f"/api/config/{args.key}", json_body={"value": clean}
            )
            if result and result[0] == 200:
                print(f"{args.key} = {json.dumps(clean)} (applied to the running backbone)")
                return 0
            print(f"API error: {result[1] if result else 'unreachable'}")
            return 1
        async with _common.Direct(boot) as direct:
            await direct.db.settings.set(args.key, clean)
        print(f"{args.key} = {json.dumps(clean)}")
        return 0

    if sub == "unset":
        if await _common.api_up(boot):
            result = await _common.api(boot, "DELETE", f"/api/config/{args.key}")
            if result and result[0] == 200:
                print(f"{args.key} reset to default")
                return 0
            # The daemon is up and owns the live value: a failed API call
            # must not fall through to the database and claim success.
            print(f"API error: {result[1] if result else 'unreachable'}")
            return 1
        async with _common.Direct(boot) as direct:
            await direct.db.settings.delete(args.key)
        print(f"{args.key} reset to default")
        return 0
    return 1


def cmd_config(args: argparse.Namespace) -> int:
    return asyncio.run(_config_cmd(args))
