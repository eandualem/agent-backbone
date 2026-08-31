"""``backbone`` command-line interface.

backbone init                 write a starter backbone.toml + .env
backbone up [--detach]        run the backbone (API + scheduler + Telegram)
backbone down                 stop a detached backbone
backbone status               show agents, sessions and service health
backbone doctor               check tmux, runtimes, config and credentials
backbone agent list|start|stop|start-all|stop-all
backbone tell <agent> <msg>   deliver a message to an agent
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import shutil
import sys
from pathlib import Path

from agent_backbone.config import CONFIG_FILENAME, BackboneConfig, find_config_file

log = logging.getLogger(__name__)

_EXAMPLE_TOML = """\
# agent-backbone configuration
# Docs: https://github.com/eandualem/agent-backbone

[backbone]
# data_dir = "~/.local/share/agent-backbone"   # SQLite db, state files, pids
# host = "127.0.0.1"
# port = 7120

# ---- Agents -------------------------------------------------------------
# One table per agent. The name is the tmux session name and the value of the
# `for:<name>` label that routes issues to it.

[agents.reviewer]
dir = "~/code/my-app"
runtime = "claude"          # claude | codex | gemini | opencode | aider | cursor | shell
# model = "claude-opus-5"
# repo = "me/my-app"        # issues opened in this repo route here automatically
# tags = ["review"]
# description = "Reviews pull requests"

# ---- GitHub task tracker (optional) -------------------------------------
# [github]
# repo = "me/my-app"        # coordination repo for `for:`-labelled issues
# mode = "webhook"          # webhook (needs GITHUB_WEBHOOK_SECRET + a public URL) or poll

# ---- Telegram (optional) -------------------------------------------------
# [telegram]
# allowed_chat_ids = [123456789]   # required — the bot ignores everyone else
# notification_chat_id = 123456789
# [telegram.topic_routes]
# 42 = "reviewer"

# ---- Escalation ----------------------------------------------------------
# [escalation]
# target = "reviewer"       # agent that hears about stalls / offline agents
# stall_threshold_seconds = 5400

# ---- Security ------------------------------------------------------------
# [security]
# allow_remote_plan_control = false   # approve/reject plans via API/Telegram
"""

_EXAMPLE_ENV = """\
# Secrets for agent-backbone (never commit this file)
BACKBONE_API_KEY={api_key}

# GitHub: a token (PAT or `gh auth token`) is the simplest option
# GITHUB_TOKEN=ghp_...
# GITHUB_WEBHOOK_SECRET=...           # required in webhook mode

# GitHub App (alternative to GITHUB_TOKEN)
# GITHUB_APP_ID=
# GITHUB_APP_PRIVATE_KEY_PATH=

# Telegram bot token from @BotFather
# TELEGRAM_TOKEN=
"""


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    target_dir = Path(args.dir).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    toml_path = target_dir / CONFIG_FILENAME
    env_path = target_dir / ".env"

    if toml_path.exists() and not args.force:
        print(f"{toml_path} already exists (use --force to overwrite)")
        return 1

    toml_path.write_text(_EXAMPLE_TOML)
    print(f"wrote {toml_path}")

    if not env_path.exists() or args.force:
        env_path.write_text(_EXAMPLE_ENV.format(api_key=secrets.token_urlsafe(32)))
        os.chmod(env_path, 0o600)
        print(f"wrote {env_path} (contains a generated BACKBONE_API_KEY)")

    print("\nNext steps:")
    print(f"  1. edit {toml_path.name}: add your agents under [agents.<name>]")
    print("  2. backbone doctor")
    print("  3. backbone up")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from agent_backbone.services.infrastructure._agents import (
        RUNTIME_COMMANDS,
        resolve_command,
    )

    ok = True

    def check(label: str, passed: bool, hint: str = "") -> None:
        nonlocal ok
        mark = "✓" if passed else "✗"
        print(f"  {mark} {label}" + (f"  — {hint}" if (hint and not passed) else ""))
        ok = ok and passed

    config_path = find_config_file()
    print("Config")
    check(
        f"backbone.toml: {config_path or 'not found'}",
        config_path is not None,
        "run `backbone init`",
    )
    try:
        config = BackboneConfig.load()
    except Exception as exc:  # pragma: no cover - defensive
        print(f"  ✗ config failed to load: {exc}")
        return 1

    check(
        f"{len(config.agents)} agent(s) configured",
        len(config.agents) > 0,
        "add [agents.<name>] tables",
    )
    for spec in config.agents:
        check(f"agent '{spec.name}' dir exists: {spec.path}", spec.path.is_dir())
        check(
            f"agent '{spec.name}' runtime '{spec.runtime}' installed",
            spec.runtime in RUNTIME_COMMANDS
            and (
                RUNTIME_COMMANDS[spec.runtime] is None
                or resolve_command(RUNTIME_COMMANDS[spec.runtime]) is not None
            ),
        )

    print("Tools")
    check("tmux on PATH", shutil.which("tmux") is not None, "install tmux")

    print("Security")
    check(
        "API key configured",
        bool(config.api_key) or config.security.allow_unauthenticated,
        "set BACKBONE_API_KEY in .env",
    )
    if config.security.allow_unauthenticated:
        print("  ! API authentication is disabled (allow_unauthenticated)")

    print("Integrations")
    if config.github.enabled:
        check(
            f"GitHub credentials for {config.github.repo}",
            config.github_ready,
            "set GITHUB_TOKEN (or GitHub App credentials)",
        )
        if config.github.mode == "webhook":
            check(
                "GITHUB_WEBHOOK_SECRET set (webhook mode)",
                bool(config.webhook_secret),
                'set GITHUB_WEBHOOK_SECRET or use mode = "poll"',
            )
    else:
        print("  - GitHub tracker not configured (optional)")
    if config.telegram_ready:
        check(
            "Telegram allowed_chat_ids set",
            bool(config.telegram.allowed_chat_ids),
            "add your chat id to [telegram] allowed_chat_ids",
        )
    else:
        print("  - Telegram not configured (optional)")

    print("Storage")
    print(f"  - data dir: {config.data_dir}")
    print(f"  - database: {config.database_url}")

    print("\nAll good." if ok else "\nSome checks failed.")
    return 0 if ok else 1


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

    uvicorn.run(
        create_app(config),
        host=config.backbone.host,
        port=config.backbone.port,
        log_level="info",
    )


async def _up_detached(config: BackboneConfig) -> int:
    from agent_backbone.services.terminal import session_exists, start_session

    session = config.backbone.session_name
    if await session_exists(session):
        print(f"backbone already running in tmux session '{session}'")
        return 0
    command = [sys.executable, "-m", "agent_backbone.cli", "up"]
    env = {"BACKBONE_CONFIG": str(config.source_path)} if config.source_path else None
    ok = await start_session(session, working_dir=str(Path.cwd()), command=command, environment=env)
    if ok:
        print(f"backbone started in tmux session '{session}' (attach: tmux attach -t {session})")
        return 0
    print("failed to start backbone session")
    return 1


def cmd_up(args: argparse.Namespace) -> int:
    config = BackboneConfig.load()
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
    return asyncio.run(_down(BackboneConfig.load()))


async def _api_get(config: BackboneConfig, path: str) -> dict | None:
    import httpx

    url = f"http://{config.backbone.host}:{config.backbone.port}{path}"
    headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json()
            print(f"API {path} returned {resp.status_code}: {resp.text[:200]}")
    except httpx.HTTPError:
        return None
    return None


async def _status(config: BackboneConfig) -> int:
    from agent_backbone.services.terminal import list_sessions

    sessions = set(await list_sessions())
    api_up = await _api_get(config, "/health")
    print(
        f"backbone API : {'up' if api_up else 'down'} (http://{config.backbone.host}:{config.backbone.port})"
    )
    if api_up:
        components = api_up.get("components", {})
        for name, comp in components.items():
            print(f"  {name:<14s} {'ok' if comp.get('healthy') else 'DEGRADED'}")

    print("\nagents:")
    if not config.agents:
        print("  (none configured)")
    width = max((len(n) for n in config.agents.names), default=8)
    for spec in config.agents:
        state = "running" if spec.name in sessions else "stopped"
        print(f"  {spec.name:<{width}s}  {state:<8s} {spec.runtime}  {spec.path}")
    others = sorted(
        s for s in sessions if s not in config.agents and s != config.backbone.session_name
    )
    if others:
        print("\nother tmux sessions: " + ", ".join(others))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    return asyncio.run(_status(BackboneConfig.load()))


async def _agent(args: argparse.Namespace, config: BackboneConfig) -> int:
    from agent_backbone.services.infrastructure import _agents

    sub = args.agent_command
    if sub == "list":
        print(_agents.list_agents(config))
        return 0
    if sub == "start":
        spec = config.agents.get(args.name)
        if spec is None:
            print(f"unknown agent '{args.name}' — configured: {', '.join(config.agents.names)}")
            return 1
        ok = await _agents.start_agent(
            spec, runtime=args.runtime, model=args.model, resume=args.resume
        )
        return 0 if ok else 1
    if sub == "stop":
        return 0 if await _agents.stop_agent(args.name) else 1
    if sub == "start-all":
        started = await _agents.start_all(config)
        print(f"started {started} agent(s)")
        return 0
    if sub == "stop-all":
        stopped = await _agents.stop_all_agents(config)
        print(f"stopped {stopped} agent(s)")
        return 0
    return 1


def cmd_agent(args: argparse.Namespace) -> int:
    return asyncio.run(_agent(args, BackboneConfig.load()))


async def _tell(args: argparse.Namespace, config: BackboneConfig) -> int:
    import httpx

    text = " ".join(args.message)
    url = f"http://{config.backbone.host}:{config.backbone.port}/api/messages"
    headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
    payload = {
        "target_session": args.agent,
        "from_entity": args.sender,
        "message": text,
        "priority": args.priority,
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        print(f"backbone API unreachable ({exc}); is `backbone up` running?")
        return 1
    if resp.status_code != 200:
        print(f"error {resp.status_code}: {resp.text[:300]}")
        return 1
    data = resp.json()
    print(json.dumps(data))
    return 0 if data.get("ok") else 2


def cmd_tell(args: argparse.Namespace) -> int:
    return asyncio.run(_tell(args, BackboneConfig.load()))


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backbone", description=__doc__.split("\n\n")[0])
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="write a starter backbone.toml and .env")
    p.add_argument("--dir", default=".", help="directory to write into (default: cwd)")
    p.add_argument("--force", action="store_true", help="overwrite existing files")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("doctor", help="check the environment and configuration")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("up", help="run the backbone")
    p.add_argument("--detach", "-d", action="store_true", help="run inside a tmux session")
    p.add_argument("--reload", action="store_true", help="auto-reload on code changes (dev)")
    p.set_defaults(func=cmd_up)

    p = sub.add_parser("down", help="stop a detached backbone")
    p.set_defaults(func=cmd_down)

    p = sub.add_parser("status", help="show agents, sessions and health")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("agent", help="manage agent sessions")
    asub = p.add_subparsers(dest="agent_command", required=True)
    asub.add_parser("list", help="list configured agents")
    ps = asub.add_parser("start", help="start a configured agent")
    ps.add_argument("name")
    ps.add_argument("--runtime", default=None)
    ps.add_argument("--model", default=None)
    ps.add_argument("--resume", action="store_true")
    pst = asub.add_parser("stop", help="stop an agent session")
    pst.add_argument("name")
    asub.add_parser("start-all", help="start every configured agent")
    asub.add_parser("stop-all", help="stop every configured agent")
    p.set_defaults(func=cmd_agent)

    p = sub.add_parser("tell", help="deliver a message to an agent (via the running API)")
    p.add_argument("agent")
    p.add_argument("message", nargs="+")
    p.add_argument("--from", dest="sender", default=os.environ.get("USER", "cli"))
    p.add_argument("--priority", action="store_true", help="bypass user-interacting checks")
    p.set_defaults(func=cmd_tell)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
