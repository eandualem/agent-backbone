"""``backbone`` command-line interface.

backbone init                        create the data directory, .env and database
backbone up [--detach]               run the backbone (API + scheduler + Telegram)
backbone down                        stop a detached backbone
backbone status                      agents, sessions, repositories and health
backbone doctor                      check tmux, runtimes, credentials
backbone config list|get|set|unset   settings (stored in the database)
backbone agent start [--dir D]       discover + start an agent (waits for its prompt)
backbone agent list|stop|inspect|set|watch|unwatch|forget
backbone tell <agent> <msg>          deliver a message to an agent
backbone hooks install claude        install the state-reporting hooks

Commands talk to the running backbone API when it is up and fall back to the
database directly when it is not.
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
from typing import Any

from agent_backbone.config import (
    SETTINGS_DEFAULTS,
    SETTINGS_HELP,
    BackboneConfig,
    bootstrap_config,
    validate_setting,
)

log = logging.getLogger(__name__)

_EXAMPLE_ENV = """\
# Secrets for agent-backbone (never commit this file)
BACKBONE_API_KEY={api_key}

# GitHub — a token (PAT or `gh auth token`) is the simplest option
# GITHUB_TOKEN=ghp_...
# GITHUB_WEBHOOK_SECRET=...           # set this and the backbone switches to webhook intake

# GitHub App (alternative to GITHUB_TOKEN)
# GITHUB_APP_ID=
# GITHUB_APP_PRIVATE_KEY_PATH=

# Telegram bot token from @BotFather
# TELEGRAM_TOKEN=
"""


# ---------------------------------------------------------------------------
# Helpers: API-first, database fallback
# ---------------------------------------------------------------------------


def _api_url(config: BackboneConfig, path: str) -> str:
    return f"http://{config.backbone.host}:{config.backbone.port}{path}"


def _headers(config: BackboneConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}


async def _api(
    config: BackboneConfig, method: str, path: str, *, json_body: Any = None, timeout: float = 10.0
) -> tuple[int, Any] | None:
    """Call the running API. Returns None when the backbone is not reachable."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(
                method, _api_url(config, path), headers=_headers(config), json=json_body
            )
    except httpx.HTTPError:
        return None
    try:
        data = resp.json()
    except ValueError:
        data = resp.text
    return resp.status_code, data


async def _api_up(config: BackboneConfig) -> bool:
    result = await _api(config, "GET", "/health", timeout=3.0)
    return result is not None


class _Direct:
    """Direct database access for when the backbone is not running."""

    def __init__(self, config: BackboneConfig) -> None:
        self._boot = config
        self.db = None
        self.store = None
        self.config = config

    async def __aenter__(self) -> _Direct:
        from agent_backbone.services.agent_store import AgentStore
        from agent_backbone.services.database import BackboneDB, DatabaseService

        self._service = DatabaseService(self._boot.database_url)
        await self._service.start()
        self.db = BackboneDB(self._service.engine)
        await self.db.start()
        self.store = AgentStore(self.db, self._boot.data_dir)
        self.config = await self.store.refresh()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.db.stop()
        await self._service.stop()


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str))


# ---------------------------------------------------------------------------
# init / doctor
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    config = bootstrap_config(args.data_dir)
    data_dir = config.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "state").mkdir(exist_ok=True)

    env_path = config.env_path
    if not env_path.exists() or args.force:
        env_path.write_text(_EXAMPLE_ENV.format(api_key=secrets.token_urlsafe(32)))
        os.chmod(env_path, 0o600)
        print(f"wrote {env_path} (contains a generated BACKBONE_API_KEY)")
    else:
        print(f"{env_path} exists (kept; use --force to regenerate)")

    async def _migrate() -> None:
        async with _Direct(config):
            pass

    asyncio.run(_migrate())
    print(f"database ready: {config.database_url}")

    print("\nNext steps:")
    print("  1. backbone doctor")
    print("  2. backbone up --detach")
    print("  3. cd ~/code/my-app && backbone agent start")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from agent_backbone.services.infrastructure._agents import RUNTIME_COMMANDS, runtime_available

    ok = True

    def check(label: str, passed: bool, hint: str = "") -> None:
        nonlocal ok
        mark = "✓" if passed else "✗"
        print(f"  {mark} {label}" + (f"  — {hint}" if (hint and not passed) else ""))
        ok = ok and passed

    async def run() -> int:
        nonlocal ok
        boot = bootstrap_config()
        print("Storage")
        check(f"data dir exists: {boot.data_dir}", boot.data_dir.is_dir(), "run `backbone init`")
        check(f".env present: {boot.env_path}", boot.env_path.is_file(), "run `backbone init`")
        try:
            async with _Direct(boot) as direct:
                config = direct.config
                check(f"database reachable: {config.database_url}", True)
        except Exception as exc:
            check(f"database reachable: {boot.database_url}", False, f"{exc}; run `backbone init`")
            return 1

        print("Agents")
        if not config.agents:
            print("  - none yet (run `backbone agent start` from a project directory)")
        for spec in config.agents:
            check(f"'{spec.name}' dir exists: {spec.path}", spec.path.is_dir())
            check(
                f"'{spec.name}' runtime '{spec.runtime}' installed", runtime_available(spec.runtime)
            )
            if not spec.repo:
                print(f"  ! '{spec.name}' has no GitHub remote — issue routing is off for it")

        print("Tools")
        check("tmux on PATH", shutil.which("tmux") is not None, "install tmux")
        installed = [r for r in RUNTIME_COMMANDS if runtime_available(r) and r != "shell"]
        print(f"  - runtimes installed: {', '.join(installed) or 'none'}")

        print("Security")
        check(
            "API key configured",
            bool(config.api_key) or config.security.allow_unauthenticated,
            "set BACKBONE_API_KEY in .env",
        )
        if config.security.allow_unauthenticated:
            print("  ! API authentication is disabled (security.allow_unauthenticated)")

        print("Integrations")
        if config.github_app_ready and not config.github_token:
            try:
                import cryptography  # noqa: F401
            except ModuleNotFoundError:
                check(
                    "GitHub App auth dependencies installed",
                    False,
                    "install the extra: uv tool install 'agent-backbone[github-app]'",
                )
            key_ok = Path(config.github_app_private_key_path).expanduser().is_file()
            check(f"GitHub App private key: {config.github_app_private_key_path}", key_ok)
        if config.github_ready:
            print(f"  ✓ GitHub credentials found — intake: {config.github_intake}")
            if config.github_intake == "poll":
                print(
                    "    (set GITHUB_WEBHOOK_SECRET + expose /webhooks/github for instant delivery)"
                )
        else:
            print("  - GitHub not configured (optional): set GITHUB_TOKEN in .env")
        if config.telegram_ready:
            check(
                "Telegram allowed_chat_ids set",
                bool(config.telegram.allowed_chat_ids),
                "backbone config set telegram.allowed_chat_ids '[<chat id>]'",
            )
        else:
            print("  - Telegram not configured (optional): set TELEGRAM_TOKEN in .env")

        print("Backbone")
        print(f"  - API: {'up' if await _api_up(config) else 'down'} ({_api_url(config, '')})")
        print("\nAll good." if ok else "\nSome checks failed.")
        return 0 if ok else 1

    return asyncio.run(run())


# ---------------------------------------------------------------------------
# up / down / status
# ---------------------------------------------------------------------------


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
        create_app(config), host=config.backbone.host, port=config.backbone.port, log_level="info"
    )


async def _load_config() -> BackboneConfig:
    """Full configuration (settings + agents) from the database."""
    async with _Direct(bootstrap_config()) as direct:
        return direct.config


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
        return 0
    print("failed to start backbone session")
    return 1


def cmd_up(args: argparse.Namespace) -> int:
    config = asyncio.run(_load_config())
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
    return asyncio.run(_down(asyncio.run(_load_config())))


async def _status() -> int:
    from agent_backbone.services.terminal import list_sessions

    config = await _load_config()
    sessions = set(await list_sessions())
    health = await _api(config, "GET", "/health", timeout=3.0)
    api_up = health is not None
    print(f"backbone API : {'up' if api_up else 'down'} ({_api_url(config, '')})")
    if api_up and isinstance(health[1], dict):
        for name, comp in health[1].get("components", {}).items():
            print(f"  {name:<14s} {'ok' if comp.get('healthy') else 'DEGRADED'}")
    print(f"github intake: {config.github_intake}")

    print("\nagents:")
    if not config.agents:
        print("  (none yet — run `backbone agent start` from a project directory)")
    states: dict[str, dict] = {}
    if api_up:
        result = await _api(config, "GET", "/api/agents")
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
            result = await _api(config, "GET", "/api/status")
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


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def _parse_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return raw


async def _config_cmd(args: argparse.Namespace) -> int:
    sub = args.config_command
    boot = bootstrap_config()

    if sub == "list":
        async with _Direct(boot) as direct:
            stored = await direct.db.get_all_settings()
        width = max(len(k) for k in SETTINGS_DEFAULTS)
        for key in sorted(SETTINGS_DEFAULTS):
            value = stored.get(key, SETTINGS_DEFAULTS[key])
            marker = "*" if key in stored else " "
            print(f"{marker} {key:<{width}s} = {json.dumps(value)}")
        print("\n(* = set explicitly; others are defaults)")
        return 0

    if sub == "get":
        async with _Direct(boot) as direct:
            stored = await direct.db.get_all_settings()
        if args.key not in SETTINGS_DEFAULTS:
            print(f"unknown setting '{args.key}'")
            return 1
        print(json.dumps(stored.get(args.key, SETTINGS_DEFAULTS[args.key])))
        if args.key in SETTINGS_HELP:
            print(f"  {SETTINGS_HELP[args.key]}")
        return 0

    if sub == "set":
        value = _parse_value(args.value)
        try:
            clean = validate_setting(args.key, value)
        except (KeyError, ValueError) as exc:
            print(f"error: {exc}")
            return 1
        if await _api_up(boot):
            result = await _api(boot, "PUT", f"/api/config/{args.key}", json_body={"value": clean})
            if result and result[0] == 200:
                print(f"{args.key} = {json.dumps(clean)} (applied to the running backbone)")
                return 0
            print(f"API error: {result[1] if result else 'unreachable'}")
            return 1
        async with _Direct(boot) as direct:
            await direct.db.set_setting(args.key, clean)
        print(f"{args.key} = {json.dumps(clean)}")
        return 0

    if sub == "unset":
        if await _api_up(boot):
            result = await _api(boot, "DELETE", f"/api/config/{args.key}")
            if result and result[0] == 200:
                print(f"{args.key} reset to default")
                return 0
        async with _Direct(boot) as direct:
            await direct.db.delete_setting(args.key)
        print(f"{args.key} reset to default")
        return 0
    return 1


def cmd_config(args: argparse.Namespace) -> int:
    return asyncio.run(_config_cmd(args))


# ---------------------------------------------------------------------------
# agent
# ---------------------------------------------------------------------------


def _print_start_result(data: dict) -> None:
    name = data.get("name") or data.get("session")
    if data.get("already_existed"):
        print(f"{name}: already running")
        return
    if not data.get("ok"):
        print(f"{name}: failed to start ({data.get('ready', 'unknown')})")
        for line in data.get("evidence", []):
            print(f"  - {line}")
        return
    ready = data.get("ready", "not_waited")
    repo = f" repo {data['repo']}" if data.get("repo") else " (no GitHub remote)"
    label = {
        "ready": "ready",
        "waiting_for_human": "started, waiting for you",
        "timeout": "started but not at its prompt yet",
        "not_waited": "started",
    }
    print(f"{name}: {label.get(ready, ready)} — {data.get('runtime')}{repo}")
    print(f"  dir: {data.get('working_directory')}")
    for line in data.get("evidence", []):
        print(f"  - {line}")
    if ready in ("timeout", "waiting_for_human"):
        print(f"  answer it there: tmux attach -t {name}")


async def _agent_start(args: argparse.Namespace) -> int:
    boot = bootstrap_config()
    if len(args.names) > 1:
        if args.dir or args.watch:
            print("--dir/--watch apply to a single agent; start a group by name only")
            return 1
        # A group start: each name must already be a known agent.
        worst = 0
        for name in args.names:
            single = argparse.Namespace(**{**vars(args), "names": [name], "group": True})
            worst = max(worst, await _agent_start(single))
        return worst
    name = args.names[0] if args.names else None
    directory = args.dir
    if directory is None and name is None:
        directory = os.getcwd()
    elif directory is None and name is not None and not getattr(args, "group", False):
        # A bare unknown name registers the current directory under that name.
        config = await _load_config()
        if config.agents.get(name) is None:
            directory = os.getcwd()
            print(f"'{name}' is new — registering it for {directory}")
    body = {
        "name": name,
        "dir": str(Path(directory).expanduser().resolve()) if directory else None,
        "runtime": args.runtime,
        "model": args.model,
        "resume": args.resume,
        "watch": args.watch or [],
        "wait": not args.no_wait,
    }

    if await _api_up(boot):
        result = await _api(boot, "POST", "/api/agents/start", json_body=body, timeout=120.0)
        if result is None:
            print("backbone API unreachable")
            return 1
        status, data = result
        if status != 200:
            print(f"error {status}: {data.get('detail') if isinstance(data, dict) else data}")
            return 1
        _print_start_result(data)
        return 0 if data.get("ok") else 1

    # Backbone not running: register + start directly.
    from agent_backbone.config import AgentSpec
    from agent_backbone.services.infrastructure._agents import start_agent, wait_until_ready

    async with _Direct(boot) as direct:
        store = direct.store
        if body["dir"]:
            spec = store.discover(body["dir"], name=name, runtime=args.runtime, model=args.model)
            if args.watch:
                spec = AgentSpec(
                    **{
                        **spec.__dict__,
                        "watches": tuple(dict.fromkeys([*spec.watches, *args.watch])),
                    }
                )
            spec = await store.register(spec)
        else:
            spec = store.agents.get(name)
            if spec is None:
                print(f"unknown agent '{name}' — pass --dir to register it")
                return 1
            # An override at start becomes the recorded setting (matches the API).
            changes: dict = {}
            if args.runtime and args.runtime != spec.runtime:
                changes["runtime"] = args.runtime
            if args.model is not None and args.model != spec.model:
                changes["model"] = args.model
            if changes:
                spec = await store.update(name, **changes)
        config = direct.config
        runtime = args.runtime or spec.runtime
        ok = await start_agent(
            spec,
            runtime=args.runtime,
            model=args.model,
            resume=args.resume,
            state_dir=config.state_dir,
            data_dir=config.data_dir,
        )
        if not ok:
            print(f"{spec.name}: failed to start")
            return 1
        await store.touch_started(spec.name)
        ready, evidence = "not_waited", []
        if not args.no_wait:
            ready, evidence = await wait_until_ready(
                spec.name,
                state_dir=config.state_dir,
                runtime=runtime,
                timeout=config.monitor.start_timeout_seconds,
            )
        _print_start_result(
            {
                "ok": ready != "exited",
                "name": spec.name,
                "runtime": runtime,
                "repo": spec.repo,
                "working_directory": str(spec.path),
                "ready": ready,
                "evidence": evidence,
            }
        )
        print(
            "note: the backbone is not running — start it with `backbone up --detach` for routing"
        )
        return 0 if ready != "exited" else 1


async def _agent(args: argparse.Namespace) -> int:
    from agent_backbone.services.infrastructure import _agents

    sub = args.agent_command
    if sub == "start":
        return await _agent_start(args)

    boot = bootstrap_config()
    api_up = await _api_up(boot)

    if sub == "list":
        config = await _load_config()
        if not config.agents:
            print("No agents known yet. Run `backbone agent start` from a project directory.")
            return 0
        width = max(len(name) for name in config.agents.names)
        for spec in config.agents:
            model = f" ({spec.model})" if spec.model else ""
            print(f"  {spec.name:<{width}s}  {spec.runtime}{model}  {spec.path}")
        return 0

    if sub == "stop":
        failed = False
        for name in args.names:
            if api_up:
                result = await _api(boot, "POST", f"/api/agents/{name}/stop", timeout=30.0)
                ok = bool(result and result[0] == 200 and result[1].get("ok"))
            else:
                ok = await _agents.stop_agent(name)
            print(f"{name}: {'stopped' if ok else 'not stopped'}")
            failed = failed or not ok
        return 1 if failed else 0

    if sub == "inspect":
        if api_up:
            result = await _api(boot, "GET", f"/api/agents/{args.name}/inspect", timeout=30.0)
            if result and result[0] == 200:
                data = result[1]
                if args.json:
                    _print_json(data)
                    return 0
                print(
                    f"{data['name']}: {'online' if data['online'] else 'offline'}"
                    f"{'' if data['known'] else ' (not a known agent)'}"
                )
                print(f"  dir:      {data['dir'] or '-'}")
                print(f"  runtime:  {data['runtime'] or '-'}   model: {data['model'] or '-'}")
                watches = ", ".join(data["watches"]) or "-"
                print(f"  repo:     {data['repo'] or '-'}   watches: {watches}")
                reason = f" ({data['reason']})" if data.get("reason") else ""
                issue = (
                    f"   on {data.get('current_repo') or ''}#{data['current_issue']}"
                    if data.get("current_issue")
                    else ""
                )
                age = (
                    f" (hook state {data['state_age_seconds']}s old)"
                    if data.get("state_age_seconds") is not None
                    else ""
                )
                print(f"  state:    {data['state']}{reason}{issue}{age}")
                print(f"  delivery: {data['delivery']}")
                print("  evidence:")
                for line in data["evidence"]:
                    print(f"    - {line}")
                if data.get("pane_tail"):
                    print("  terminal tail:")
                    for line in data["pane_tail"]:
                        print(f"    | {line}")
                if data.get("recent_deliveries"):
                    print("  recent deliveries:")
                    for d in data["recent_deliveries"][:5]:
                        ref = (
                            f"{d.get('repo') or ''}#{d['issue_number']}"
                            if d.get("issue_number")
                            else d.get("kind")
                        )
                        print(f"    {d['created_at'][:19]}  {ref:<24s} {d['outcome']}")
                return 0
            print(f"error: {result[1] if result else 'API unreachable'}")
            return 1
        # Offline inspection: state file + tmux only.
        from agent_backbone.services.agents._inference import get_agent_state
        from agent_backbone.services.terminal import session_exists

        config = await _load_config()
        online = await session_exists(args.name)
        snapshot = await get_agent_state(
            config.state_dir, args.name, config.agent_state.stale_threshold_seconds
        )
        print(f"{args.name}: {'online' if online else 'offline'} (backbone not running)")
        print(
            f"  state: {snapshot.state.value}{f' ({snapshot.reason})' if snapshot.reason else ''}"
        )
        for line in snapshot.evidence:
            print(f"    - {line}")
        return 0

    if sub == "set":
        changes: dict[str, Any] = {}
        for item in args.assignments:
            if "=" not in item:
                print(f"expected key=value, got {item!r}")
                return 1
            key, raw = item.split("=", 1)
            changes[key] = _parse_value(raw) if key in ("tags", "env") else raw
        if api_up:
            result = await _api(boot, "PATCH", f"/api/agents/{args.name}", json_body=changes)
            if result and result[0] == 200:
                _print_json(result[1])
                return 0
            print(f"error: {result[1] if result else 'API unreachable'}")
            return 1
        async with _Direct(boot) as direct:
            try:
                spec = await direct.store.update(args.name, **changes)
            except (KeyError, ValueError) as exc:
                print(f"error: {exc}")
                return 1
        print(f"{spec.name}: updated")
        return 0

    if sub in ("watch", "unwatch"):
        # Repositories contain a slash, agent names cannot — so the name is
        # optional and, inside an agent session, defaults to the agent itself.
        repos = [t for t in args.targets if "/" in t]
        names = [t for t in args.targets if "/" not in t]
        if len(names) > 1:
            print("give at most one agent name")
            return 1
        name = names[0] if names else os.environ.get("BACKBONE_AGENT", "").strip()
        if not name:
            print(f"usage: backbone agent {sub} [NAME] OWNER/REPO…")
            print("(without NAME, $BACKBONE_AGENT must be set — it is inside agent sessions)")
            return 1
        if not repos:
            print("no repositories given — expected OWNER/REPO")
            return 1
        for repo in repos:
            if api_up:
                result = await _api(
                    boot, "POST", f"/api/agents/{name}/{sub}", json_body={"repo": repo}
                )
                if not result or result[0] != 200:
                    print(f"error: {result[1] if result else 'API unreachable'}")
                    return 1
            else:
                async with _Direct(boot) as direct:
                    try:
                        if sub == "watch":
                            await direct.store.watch(name, repo)
                        else:
                            await direct.store.unwatch(name, repo)
                    except KeyError:
                        print(f"unknown agent '{name}'")
                        return 1
            print(f"{name}: {'now watching' if sub == 'watch' else 'stopped watching'} {repo}")
        return 0

    if sub == "forget":
        if api_up:
            result = await _api(boot, "DELETE", f"/api/agents/{args.name}")
            if result and result[0] == 200:
                print(f"{args.name}: forgotten")
                return 0
            print(f"error: {result[1] if result else 'API unreachable'}")
            return 1
        async with _Direct(boot) as direct:
            removed = await direct.store.forget(args.name)
        print(f"{args.name}: {'forgotten' if removed else 'unknown agent'}")
        return 0 if removed else 1

    return 1


def cmd_agent(args: argparse.Namespace) -> int:
    return asyncio.run(_agent(args))


# ---------------------------------------------------------------------------
# tell / hooks
# ---------------------------------------------------------------------------


async def _tell(args: argparse.Namespace) -> int:
    boot = bootstrap_config()
    text = " ".join(args.message)
    payload = {
        "target_session": args.agent,
        "from_entity": args.sender,
        "message": text,
        "priority": args.priority,
    }
    result = await _api(boot, "POST", "/api/messages", json_body=payload, timeout=30.0)
    if result is None:
        print("backbone API unreachable; is `backbone up` running?")
        return 1
    status, data = result
    if status != 200:
        print(f"error {status}: {data}")
        return 1
    print(json.dumps(data))
    if not data.get("ok") and data.get("queued"):
        print(f"queued — delivered when the agent is ready (blocked: {data.get('outcome')})")
    return 0 if data.get("ok") else 2


def cmd_tell(args: argparse.Namespace) -> int:
    return asyncio.run(_tell(args))


def cmd_hooks(args: argparse.Namespace) -> int:
    from agent_backbone.hooks import install as hooks

    config = bootstrap_config()
    if args.runtime != "claude":
        print(f"hooks are only available for 'claude' right now (got {args.runtime!r})")
        return 1
    project_dir = Path(args.dir).expanduser() if args.dir else None
    if args.hooks_command == "install":
        settings_path, command = hooks.install_claude(
            config.data_dir,
            config.state_dir,
            project_dir=project_dir,
            python=hooks.default_python(),
        )
        print(f"installed Claude Code hooks in {settings_path}")
        print(f"  command: {command}")
        print(f"  state:   {config.state_dir}")
        print("Restart running Claude Code sessions for the hooks to take effect.")
        return 0
    if args.hooks_command == "uninstall":
        settings_path = hooks.uninstall_claude(project_dir=project_dir)
        print(f"removed agent-backbone hooks from {settings_path}")
        return 0
    return 1


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backbone", description=__doc__.split("\n\n")[0])
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create the data directory, .env and database")
    p.add_argument(
        "--data-dir",
        default=None,
        help="data directory (default: $BACKBONE_DATA_DIR or ~/.local/share/agent-backbone)",
    )
    p.add_argument("--force", action="store_true", help="regenerate .env")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("doctor", help="check the environment and configuration")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("up", help="run the backbone")
    p.add_argument("--detach", "-d", action="store_true", help="run inside a tmux session")
    p.add_argument("--reload", action="store_true", help="auto-reload on code changes (dev)")
    p.set_defaults(func=cmd_up)

    p = sub.add_parser("down", help="stop a detached backbone")
    p.set_defaults(func=cmd_down)

    p = sub.add_parser("status", help="show agents, repositories, sessions and health")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("config", help="settings (stored in the database)")
    csub = p.add_subparsers(dest="config_command", required=True)
    csub.add_parser("list", help="show every setting")
    pc = csub.add_parser("get", help="show one setting")
    pc.add_argument("key")
    pc = csub.add_parser("set", help="change a setting (JSON values accepted)")
    pc.add_argument("key")
    pc.add_argument("value")
    pc = csub.add_parser("unset", help="reset a setting to its default")
    pc.add_argument("key")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("agent", help="manage agents")
    asub = p.add_subparsers(dest="agent_command", required=True)
    asub.add_parser("list", help="list known agents")
    ps = asub.add_parser("start", help="start agents (discovers a new one from a directory)")
    ps.add_argument(
        "names",
        nargs="*",
        default=[],
        metavar="NAME",
        help="agent name(s); default: discover from the current directory",
    )
    ps.add_argument(
        "--dir", default=None, help="project directory (default: cwd when no name is given)"
    )
    ps.add_argument(
        "--runtime",
        default=None,
        help="claude | codex | gemini | opencode | aider | cursor | shell "
        "(default: agents.default_runtime, recorded on the agent afterwards)",
    )
    ps.add_argument(
        "--model",
        default=None,
        help="model passed to the runtime CLI (e.g. opus, sonnet, or a full model id); "
        "recorded on the agent and reused by later starts",
    )
    ps.add_argument("--resume", action="store_true", help="resume the runtime's last conversation")
    ps.add_argument(
        "--watch",
        action="append",
        default=None,
        metavar="OWNER/REPO",
        help="also watch this repository (repeatable)",
    )
    ps.add_argument(
        "--no-wait",
        action="store_true",
        help="return immediately instead of waiting for the prompt",
    )
    pst = asub.add_parser("stop", help="stop agent sessions")
    pst.add_argument("names", nargs="+", metavar="NAME")
    pi = asub.add_parser("inspect", help="show state, delivery readiness and the evidence")
    pi.add_argument("name")
    pi.add_argument("--json", action="store_true")
    pse = asub.add_parser(
        "set", help="change agent fields: runtime=… model=… repo=… dir=… description=…"
    )
    pse.add_argument("name")
    pse.add_argument("assignments", nargs="+", metavar="key=value")
    pw = asub.add_parser("watch", help="watch repositories (NAME optional inside an agent session)")
    pw.add_argument("targets", nargs="+", metavar="[NAME] OWNER/REPO")
    pu = asub.add_parser(
        "unwatch", help="stop watching repositories (NAME optional inside an agent session)"
    )
    pu.add_argument("targets", nargs="+", metavar="[NAME] OWNER/REPO")
    pf = asub.add_parser("forget", help="remove an agent from the backbone")
    pf.add_argument("name")
    p.set_defaults(func=cmd_agent)

    p = sub.add_parser("hooks", help="install runtime hooks that report agent state")
    hsub = p.add_subparsers(dest="hooks_command", required=True)
    for name, help_text in (
        ("install", "add the hooks to the runtime's settings"),
        ("uninstall", "remove the hooks"),
    ):
        hp = hsub.add_parser(name, help=help_text)
        hp.add_argument("runtime", choices=["claude"], help="runtime to configure")
        hp.add_argument(
            "--dir",
            default=None,
            help="project directory (writes .claude/settings.json there); default: global",
        )
    p.set_defaults(func=cmd_hooks)

    p = sub.add_parser("tell", help="deliver a message to an agent (via the running API)")
    p.add_argument("agent")
    p.add_argument("message", nargs="+")
    p.add_argument(
        "--from",
        dest="sender",
        # Inside an agent session the sender is the agent, not the human account.
        default=os.environ.get("BACKBONE_AGENT") or os.environ.get("USER", "cli"),
        help="sender label for the provenance envelope (default: $BACKBONE_AGENT, then $USER)",
    )
    p.add_argument("--priority", action="store_true", help="deliver even while someone is typing")
    p.set_defaults(func=cmd_tell)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
