"""``backbone up|down|status|config`` — running and inspecting the backbone."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from agent_backbone.cli import _common
from agent_backbone.config import (
    SETTINGS_DEFAULTS,
    SETTINGS_HELP,
    BackboneConfig,
    validate_setting,
)

log = logging.getLogger(__name__)


def _run_server(config: BackboneConfig, reload: bool = False) -> None:
    import uvicorn

    if reload:
        uvicorn.run(
            "agent_backbone.api.app:create_app",
            factory=True,
            host=config.backbone.host,
            port=config.backbone.port,
            reload=True,
            reload_dirs=["src"],
            log_level="info",
        )
        return

    from agent_backbone.api.app import create_app

    app = create_app(config)
    uvicorn.run(app, host=config.backbone.host, port=config.backbone.port, log_level="info")
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


async def _up_detached(config: BackboneConfig) -> int:
    from agent_backbone.services.terminal import session_exists, start_session

    session = config.backbone.session_name
    if await session_exists(session):
        print(f"backbone already running in tmux session '{session}'")
        return 0
    command = [sys.executable, "-m", "agent_backbone.cli", "up"]
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
    if args.detach:
        return asyncio.run(_up_detached(config))
    _run_server(config, reload=args.reload)
    return 0


async def _down(config: BackboneConfig) -> int:
    from agent_backbone.services.terminal import graceful_close, session_exists

    session = config.backbone.session_name
    if not await session_exists(session):
        print("backbone is not running in tmux")
        return 0
    await graceful_close(session, timeout=15.0)
    print("backbone stopped")
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    return asyncio.run(_down(asyncio.run(_common.load_config())))


async def _status() -> int:
    from agent_backbone.services.terminal import list_sessions

    config = await _common.load_config()
    sessions = set(await list_sessions())
    health = await _common.api(config, "GET", "/health", timeout=3.0)
    api_up = health is not None
    print(f"backbone API : {'up' if api_up else 'down'} ({_common.api_url(config, '')})")
    if api_up and isinstance(health[1], dict):
        for name, comp in health[1].get("components", {}).items():
            print(f"  {name:<14s} {'ok' if comp.get('healthy') else 'DEGRADED'}")
    print(f"github intake: {config.github_intake}")

    print("\nagents:")
    if not config.agents:
        print("  (none yet — run `backbone agent start` from a project directory)")
    states: dict[str, dict] = {}
    if api_up:
        result = await _common.api(config, "GET", "/api/agents")
        if result and result[0] == 200:
            states = {a["name"]: a for a in result[1].get("items", [])}
    width = max((len(n) for n in config.agents.names), default=8)
    for spec in config.agents:
        live = states.get(spec.name, {})
        if spec.name in sessions:
            state = live.get("state") or "running"
            if live.get("reason"):
                state += f"({live['reason']})"
        else:
            state = "offline"
        watches = f"  watches {', '.join(spec.watches)}" if spec.watches else ""
        repo = spec.repo or "-"
        print(
            f"  {spec.name:<{width}s}  {state:<18s} {spec.runtime:<8s} {repo:<28s} "
            f"{spec.path}{watches}"
        )
    others = sorted(
        s for s in sessions if s not in config.agents and s != config.backbone.session_name
    )
    if others:
        print("\nother tmux sessions: " + ", ".join(others))

    if config.agents.repos:
        print("\nrepositories:")
        last: dict[str, str] = {}
        if api_up:
            result = await _common.api(config, "GET", "/api/status")
            if result and result[0] == 200:
                last = {
                    r["repo"]: r.get("last_event_at") or "-" for r in result[1].get("repos", [])
                }
        for repo in config.agents.repos:
            owners = ", ".join(s.name for s in config.agents.owners(repo)) or "-"
            watchers = ", ".join(s.name for s in config.agents.watchers(repo))
            line = f"  {repo:<30s} owner: {owners}"
            if watchers:
                line += f"  watchers: {watchers}"
            if last.get(repo):
                line += f"  last event: {last[repo]}"
            print(line)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    return asyncio.run(_status())


async def _config_cmd(args: argparse.Namespace) -> int:
    sub = args.config_command
    boot = await _common.client_config()

    if sub == "list":
        async with _common.Direct(boot) as direct:
            stored = await direct.db.settings.all()
        width = max(len(k) for k in SETTINGS_DEFAULTS)
        for key in sorted(SETTINGS_DEFAULTS):
            value = stored.get(key, SETTINGS_DEFAULTS[key])
            marker = "*" if key in stored else " "
            print(f"{marker} {key:<{width}s} = {json.dumps(value)}")
        print("\n(* = set explicitly; others are defaults)")
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
        async with _common.Direct(boot) as direct:
            await direct.db.settings.delete(args.key)
        print(f"{args.key} reset to default")
        return 0
    return 1


def cmd_config(args: argparse.Namespace) -> int:
    return asyncio.run(_config_cmd(args))
