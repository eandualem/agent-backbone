"""``backbone up|down|status|config`` — running and inspecting the backbone."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import subprocess
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
    resolve_data_dir,
    validate_setting,
)

log = logging.getLogger(__name__)

SHUTDOWN_DRAIN_SECONDS = 10
"""An in-flight request, such as an agent start waiting for readiness, must not hold a
stop or upgrade open. Sessions in tmux are unaffected."""


class UTCFormatter(DiagnosticFormatter):
    """``2026-10-13T12:50:01.123Z``: every log line can be placed in time."""

    converter = time.gmtime
    default_time_format = "%Y-%m-%dT%H:%M:%S"
    default_msec_format = "%s.%03dZ"


def _diagnostic_filter(mode: str = "off", verbose: bool = False) -> logging.Filter:
    """The shared log policy, set to the mode this log configuration carries."""
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


def _native_environments(root: Path) -> dict:
    """Capture each required runtime in the profile's one home, where each keeps its own
    store. Every other adapter is refused, and so is a runtime that is not installed:
    nothing falls back to the host's."""
    from agent_backbone.services.runtimes import RUNTIMES

    captured = {}
    for runtime in (rid for rid, rt in RUNTIMES.items() if rt.runs_in_profiles):
        try:
            environment = RUNTIMES[runtime].capture_environment(root)
        except ValueError:
            continue
        environment.create_directories()
        captured[runtime] = environment
    return captured


def _clear_inherited_signal_mask() -> None:
    """Blocked signals survive exec. A caller with SIGTERM blocked would leave a service
    that can never be stopped, and hand the same mask to everything it starts."""
    signal.pthread_sigmask(signal.SIG_SETMASK, ())


def serve_profile(registry_path: Path, profile_id: str) -> None:
    """Run one ready profile's provider with nothing ambient."""
    _clear_inherited_signal_mask()
    from agent_backbone.config import AgentsConfig, build_config, sqlite_url
    from agent_backbone.services.agents import install_project_ownership
    from agent_backbone.services.database import (
        BackboneDB,
        ProfileRegistry,
        reconcile_profile_provider,
    )
    from agent_backbone.services.runtimes import (
        RUNTIMES,
        install_native_environments,
        prepare_profile_keychain,
    )
    from agent_backbone.services.terminal import pin_tmux_server

    registry = ProfileRegistry(registry_path)
    reservation = registry.ready_reservation(profile_id)
    if reservation.kind != "isolated":
        raise ValueError("The Default profile starts with `backbone up`")
    root = reservation.data_dir
    # Replacing the process environment keeps host tokens out of load_secrets and
    # the tmux server, and the user's tmux.conf unread.
    process_environment = RUNTIMES["shell"].capture_environment(root)
    native = _native_environments(root)
    try:
        prepare_profile_keychain(root, [Path(process_environment["HOME"])])
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        log.warning("Sign-ins that use the macOS keychain will fail in this profile: %s", exc)
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
    log_file: Path | None = None,
    provider=None,
) -> None:
    import uvicorn

    from agent_backbone.api.local_transport import LocalPeerProtocol, private_listener

    logging_config = log_config(config, log_file)
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
            timeout_graceful_shutdown=SHUTDOWN_DRAIN_SECONDS,
            http=LocalPeerProtocol if provider else "auto",
        )
    )
    inner = getattr(app, "other_asgi_app", app)
    # An internal upgrade must not send SIGTERM: Uvicorn replays captured
    # signals after shutdown, which would kill us before the exec below.
    inner.state.request_shutdown = lambda: setattr(server, "should_exit", True)
    if provider is not None:
        inner.state.profile_provider = provider
    endpoint = provider.reservation.binding.endpoint if provider else None
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
        _reexec(provider)


def _reexec(provider) -> None:
    command = [
        sys.executable,
        *(["-I"] if sys.flags.isolated else []),
        "-B",
        "-m",
        "agent_backbone.cli",
        *sys.argv[1:],
    ]
    if provider is not None and provider.reservation.kind == "isolated":
        # Start over as ProviderServices.start did: CLI lookup must not move into
        # the private HOME this process installed for itself.
        kept = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL") if k in os.environ}
        os.execve(sys.executable, command, kept)
    else:
        os.execv(sys.executable, command)


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


async def _default_provider(registry, reservation):
    """`up` serves the Default profile on the tmux server recorded when it was created."""
    from agent_backbone.services.database import reconcile_profile_provider
    from agent_backbone.services.terminal import pin_tmux_server

    socket = reservation.binding.tmux_socket
    provider = await reconcile_profile_provider(
        registry, reservation, reservation.data_dir / SQLITE_FILENAME, socket
    )
    # tmux makes this directory itself unless a socket is named, and a restart clears /tmp.
    socket.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    pin_tmux_server(socket)
    return provider


def cmd_up(args: argparse.Namespace) -> int:
    from agent_backbone.services.database import default_profile_at

    _clear_inherited_signal_mask()
    # A quoted `~/…` reaches us unexpanded; a detached run starts elsewhere.
    log_file = args.log_file.expanduser().resolve() if args.log_file else None
    try:
        # Before anything can create a database or report a detached start.
        registry, reservation = default_profile_at(resolve_data_dir())
        config = asyncio.run(_common.load_config())
        if args.detach:
            _common.pin_default_terminal()
            return asyncio.run(_up_detached(config, log_file))
        provider = asyncio.run(_default_provider(registry, reservation))
        _run_server(config, log_file=log_file, provider=provider)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
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
    _common.pin_default_terminal()
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
