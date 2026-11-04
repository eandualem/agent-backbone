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

from agent_backbone.base import DiagnosticFormatter, log_policy, set_log_mode
from agent_backbone.cli import _common
from agent_backbone.cli.presentation import note, print_table
from agent_backbone.config import (
    SETTINGS_DEFAULTS,
    SETTINGS_HELP,
    SQLITE_FILENAME,
    BackboneConfig,
    validate_setting,
)

log = logging.getLogger(__name__)


class UTCFormatter(DiagnosticFormatter):
    """``2026-10-13T12:50:01.123Z``: every log line can be placed in time."""

    converter = time.gmtime
    default_time_format = "%Y-%m-%dT%H:%M:%S"
    default_msec_format = "%s.%03dZ"


def _diagnostic_filter(mode: str = "off", verbose: bool = False) -> logging.Filter:
    """Restore explicit policy in Uvicorn's fresh reload child."""
    set_log_mode(mode)
    log_policy.verbose = verbose
    return log_policy


def log_config(config: BackboneConfig, log_file: Path | None) -> dict:
    """One timestamped format for the backbone and uvicorn (its access lines too),
    written to ``log_file`` with size-based rotation, else to stderr."""
    set_log_mode(config.settings.get("diagnostics.mode", "standard"))
    if log_file is None:
        handler: dict = {"class": "logging.StreamHandler", "stream": "ext://sys.stderr"}
    else:
        handler = {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(log_file),
            "maxBytes": max(0, config.settings.get("backbone.log_max_mb", 20)) * 1024 * 1024,
            "backupCount": max(1, config.settings.get("backbone.log_backups", 3)),
            "encoding": "utf-8",
            "delay": True,
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
        "filters": {
            "diagnostics": {
                "()": _diagnostic_filter,
                "mode": log_policy.mode,
                "verbose": log_policy.verbose,
            }
        },
        "handlers": {"default": {**handler, "formatter": "utc", "filters": ["diagnostics"]}},
        # The handler applies the current mode, including after a settings change.
        "root": {"handlers": ["default"], "level": "WARNING"},
        "loggers": {
            name: {
                "handlers": [],
                "propagate": True,
                "level": (
                    "DEBUG"
                    if name == "agent_backbone"
                    else "INFO"
                    if name.startswith("uvicorn")
                    else "WARNING"
                ),
            }
            for name in (
                "agent_backbone",
                "uvicorn",
                "uvicorn.error",
                "uvicorn.access",
                "socketio",
                "socketio.server",
                "engineio",
                "engineio.server",
            )
        },
    }


def _reload_app():
    """Build the reload child with the endpoint pinned by its parent listener."""
    from dataclasses import replace

    from agent_backbone.api.app import create_app
    from agent_backbone.config import bootstrap_config

    config = bootstrap_config()
    host = os.environ.get("BACKBONE_HOST", config.backbone.host)
    return create_app(replace(config, backbone=replace(config.backbone, host=host)))


def _native_environments(root: Path) -> dict:
    """Capture each required runtime under its own root. Every other adapter is refused,
    and so is a runtime that is not installed: nothing falls back to the host's."""
    from agent_backbone.services.runtimes import RUNTIMES

    captured = {}
    for runtime in ("shell", "claude", "codex", "opencode"):
        try:
            environment = RUNTIMES[runtime].capture_environment(root / "native" / runtime)
        except ValueError:
            continue
        environment.create_directories()
        captured[runtime] = environment
    return captured


def serve_profile(registry_path: Path, profile_id: str) -> None:
    """Run one ready profile's provider with nothing ambient."""
    from agent_backbone.config import AgentsConfig, build_config, sqlite_url
    from agent_backbone.services.agents import install_project_ownership
    from agent_backbone.services.database import (
        BackboneDB,
        ProfileRegistry,
        reconcile_profile_provider,
    )
    from agent_backbone.services.runtimes import RUNTIMES, install_native_environments
    from agent_backbone.services.terminal import pin_tmux_server

    registry = ProfileRegistry(registry_path)
    reservation = registry.ready_reservation(profile_id)
    if reservation.kind != "isolated":
        raise ValueError("The adopted installation starts with `backbone up`")
    root = reservation.data_dir
    # Replacing the process environment keeps host tokens out of load_secrets and
    # the tmux server, and the user's tmux.conf unread.
    process_environment = RUNTIMES["shell"].capture_environment(root)
    native = _native_environments(root)
    os.environ.clear()
    os.environ.update(process_environment, BACKBONE_DATA_DIR=str(root))

    async def reconcile():
        provider = await reconcile_profile_provider(
            registry, reservation, root / SQLITE_FILENAME, reservation.binding.tmux_socket
        )
        pin_tmux_server(provider.socket_path)
        install_native_environments(native)
        await install_project_ownership(provider)
        async with BackboneDB.connect(sqlite_url(root)) as db:
            return provider, await db.settings.all()

    provider, settings = asyncio.run(reconcile())
    config = build_config(root, settings=settings, agents=AgentsConfig())
    _run_server(config, provider=provider)


def _run_server(
    config: BackboneConfig,
    reload: bool = False,
    log_file: Path | None = None,
    provider=None,
) -> None:
    import uvicorn

    from agent_backbone.api.local_transport import LocalPeerProtocol, private_listener

    logging_config = log_config(config, log_file)
    if reload and (config.backbone.socket_path or provider):
        raise ValueError("the private socket requires backbone up without --reload")
    if reload:
        # The reloader imports the factory in a child instead of passing config.
        # Give that factory the same fixed endpoint as the listener it serves.
        endpoint = {
            "BACKBONE_HOST": config.backbone.host,
            "BACKBONE_PORT": str(config.backbone.port),
        }
        previous = {key: os.environ.get(key) for key in endpoint}
        os.environ.update(endpoint)
        try:
            uvicorn.run(
                "agent_backbone.cli.server:_reload_app",
                factory=True,
                host=config.backbone.host,
                port=config.backbone.port,
                reload=True,
                reload_dirs=["src"],
                log_level="info",
                log_config=logging_config,
            )
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
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
            http=LocalPeerProtocol if config.backbone.socket_path or provider else "auto",
        )
    )
    inner = getattr(app, "other_asgi_app", app)
    # An internal upgrade must not send SIGTERM: Uvicorn replays captured
    # signals after shutdown, which would kill us before the exec below.
    inner.state.request_shutdown = lambda: setattr(server, "should_exit", True)
    if provider is not None:
        inner.state.profile_provider = provider
    endpoint = provider.reservation.binding.endpoint if provider else config.backbone.socket_path
    try:
        if endpoint:
            with private_listener(config.data_dir, str(endpoint)) as local:
                tcp = server.config.bind_socket()
                try:
                    # Uvicorn marks prebound sockets inheritable for worker
                    # processes. We run one worker; runtime children must not
                    # retain the port across a graceful upgrade.
                    tcp.set_inheritable(False)
                    server.run(sockets=[tcp, local])
                finally:
                    tcp.close()
        else:
            server.run()
    except KeyboardInterrupt:
        return
    if not server.started:
        raise SystemExit(3)  # preserve uvicorn.run's startup-failure exit status
    if getattr(getattr(server, "lifespan", None), "shutdown_failed", False) is True:
        raise SystemExit(3)  # never re-exec over unfinished resource cleanup
    if restart_requested(app):
        # The upgrade watch asked for new code: become a fresh `backbone up`
        # in place, so the login service or tmux session is unchanged.
        log.warning("restarting onto the new code")
        os.execv(
            sys.executable,
            [
                sys.executable,
                *(["-I"] if sys.flags.isolated else []),
                "-B",
                "-m",
                "agent_backbone.cli",
                *sys.argv[1:],
            ],
        )


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
    command = [
        sys.executable,
        *(["-I"] if sys.flags.isolated else []),
        "-B",
        "-m",
        "agent_backbone.cli",
        "up",
    ]
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


async def _adopt_on_start(registry_path: Path, root: Path):
    """The default installation becomes Default on its own; a refusal serves as before."""
    from agent_backbone.config import DEFAULT_DATA_DIR
    from agent_backbone.services.database import adopt_on_start

    if root != Path(DEFAULT_DATA_DIR).expanduser().resolve():
        return None
    try:
        # A first start has no installation identity yet.
        async with _common.Direct(_common.bootstrap_config()) as direct:
            await direct.db.signing.audience()
        return await asyncio.to_thread(adopt_on_start, registry_path, root)
    except Exception as exc:
        print(f"Default profile not created, serving as before: {exc}", file=sys.stderr)
        return None


async def _adopted_provider(config: BackboneConfig):
    """The adopted installation serves as itself; anything else stays legacy."""
    from agent_backbone.config import PROFILE_REGISTRY_PATH
    from agent_backbone.services.database import (
        ProfileRegistry,
        default_tmux_socket,
        reconcile_profile_provider,
        record_identity,
    )
    from agent_backbone.services.terminal import pin_tmux_server

    path, root = Path(PROFILE_REGISTRY_PATH).expanduser(), config.data_dir.resolve()
    registry = ProfileRegistry(path)
    reservation = registry.adopted_reservation(root)
    if reservation is None:
        reservation = await _adopt_on_start(path, root)
        if reservation is None:
            return None
    # Finishes an adoption a crash interrupted; a no-op otherwise.
    record_identity(reservation.data_dir / SQLITE_FILENAME, reservation.identity)
    socket = reservation.binding.tmux_socket
    if socket != default_tmux_socket():
        raise ValueError("The adopted installation's tmux server changed since its adoption")
    provider = await reconcile_profile_provider(
        registry, reservation, reservation.data_dir / SQLITE_FILENAME, socket
    )
    # tmux makes this directory itself unless a socket is named, and a restart clears /tmp.
    socket.parent.mkdir(mode=0o700, exist_ok=True)
    pin_tmux_server(socket)
    return provider


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
    try:
        provider = asyncio.run(_adopted_provider(config))
        _run_server(config, reload=args.reload, log_file=log_file, provider=provider)
    except SystemExit as exc:
        if exc.code and log_policy.mode == "off":
            print(
                f"Backbone failed to start or run (exit {exc.code}); optional logging is off.",
                file=sys.stderr,
            )
        raise
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
    remote = _common.is_agent_client(boot.api_key) and sub in {"list", "get"}
    if remote:
        response = await _common.api(boot, "GET", "/api/config")
        if not response or response[0] != 200:
            print("Configuration reads require the running Backbone API")
            return 1
        stored = {key: item["value"] for key, item in response[1].items()}

    if sub == "list":
        if not remote:
            async with _common.Direct(boot) as direct:
                stored = await direct.db.settings.all()
        print_table(
            "Configuration",
            ("Setting", "Value", "Source"),
            [
                (
                    key,
                    json.dumps(stored.get(key, SETTINGS_DEFAULTS[key])),
                    "API" if remote else "explicit" if key in stored else "default",
                )
                for key in sorted(SETTINGS_DEFAULTS)
            ],
        )
        note("Setting help: backbone config get KEY")
        return 0

    if sub == "get":
        if not remote:
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
