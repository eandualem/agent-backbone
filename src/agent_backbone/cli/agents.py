"""``backbone agent …``, ``tell``, ``reply`` and ``hooks`` — working with agents."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from agent_backbone.cli import _common
from agent_backbone.config import (
    bootstrap_config,
)

log = logging.getLogger(__name__)


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


def always_on_names(config) -> list[str]:
    """The agents expected to stay up — what ``agent start --always-on`` brings back."""
    return [spec.name for spec in config.agents if spec.always_on]


async def _agent_start(args: argparse.Namespace) -> int:
    boot = await _common.client_config()
    if getattr(args, "always_on", False):
        if args.names or args.dir:
            print("--always-on selects the always_on agents itself; do not pass names or --dir")
            return 1
        names = always_on_names(await _common.load_config())
        if not names:
            print("no always_on agents (set one with `backbone agent set NAME always_on=true`)")
            return 0
        print(f"starting always_on agents: {', '.join(names)}")
        args = argparse.Namespace(**{**vars(args), "names": names, "always_on": False})
        if len(names) == 1:
            return await _agent_start(argparse.Namespace(**{**vars(args), "group": True}))
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
        config = await _common.load_config()
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

    if await _common.api_up(boot):
        result = await _common.api(boot, "POST", "/api/agents/start", json_body=body, timeout=120.0)
        if result is None:
            print("backbone API unreachable")
            return 1
        status, data = result
        if status != 200:
            print(f"error {status}: {data.get('detail') if isinstance(data, dict) else data}")
            return 1
        _print_start_result(data)
        return 0 if data.get("ok") else 1

    # Backbone not running: register + start directly, through the same
    # operations the API uses.
    from agent_backbone.services.agents.operations import (
        StartRequest,
        resolve_agent,
        start_resolved,
    )

    async with _common.Direct(boot) as direct:
        req = StartRequest(
            name=name,
            directory=body["dir"],
            runtime=args.runtime,
            model=args.model,
            resume=args.resume,
            watch=tuple(args.watch or ()),
            wait=not args.no_wait,
        )
        try:
            spec = await resolve_agent(direct.store, req)
            result = await start_resolved(direct.store, direct.config, spec, req, db=direct.db)
        except KeyError as exc:
            print(f"unknown agent '{exc.args[0]}' — pass --dir to register it")
            return 1
        except ValueError as exc:
            print(f"error: {exc}")
            return 1
        if not result.ok:
            print(f"{spec.name}: failed to start")
            for line in result.evidence:
                print(f"  - {line}")
            return 1
        _print_start_result(
            {
                "ok": result.ready != "exited",
                "name": spec.name,
                "runtime": args.runtime or spec.runtime,
                "repo": spec.repo,
                "working_directory": str(spec.path),
                "already_existed": result.already_running,
                "ready": result.ready,
                "evidence": list(result.evidence),
            }
        )
        print(
            "note: the backbone is not running — start it with `backbone up --detach` for routing"
        )
        return 0 if result.ready != "exited" else 1


async def _agent(args: argparse.Namespace) -> int:
    from agent_backbone.services.agents.operations import forget_agent, stop_agent_session

    sub = args.agent_command
    if sub == "start":
        return await _agent_start(args)

    boot = await _common.client_config()
    api_up = await _common.api_up(boot)

    if sub == "list":
        config = boot
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
            if name == boot.backbone.session_name:
                print(f"{name}: not stopped (refusing to stop the backbone's own session)")
                failed = True
                continue
            if api_up:
                result = await _common.api(boot, "POST", f"/api/agents/{name}/stop", timeout=30.0)
                ok = bool(
                    result
                    and result[0] == 200
                    and isinstance(result[1], dict)
                    and result[1].get("ok")
                )
            else:
                try:
                    ok = await stop_agent_session(boot, name)
                except ValueError as exc:
                    print(f"{name}: not stopped ({exc})")
                    ok = False
            print(f"{name}: {'stopped' if ok else 'not stopped'}")
            failed = failed or not ok
        return 1 if failed else 0

    if sub in ("approve", "deny"):
        if not api_up:
            print("backbone API unreachable; is `backbone up` running?")
            return 1
        result = await _common.api(
            boot,
            "POST",
            f"/api/agents/{args.name}/{sub}",
            json_body={"from_entity": args.sender},
            timeout=30.0,
        )
        if result is None:
            print("backbone API unreachable; is `backbone up` running?")
            return 1
        status, data = result
        detail = data.get("detail") if isinstance(data, dict) else None
        if status != 200:
            if isinstance(detail, dict):
                print(f"{args.name}: {detail.get('outcome', 'error')}")
                for line in detail.get("evidence", []):
                    print(f"    | {line}")
            else:
                print(f"error {status}: {detail or data}")
            return 1
        verb = "approved" if sub == "approve" else "denied"
        print(f"{args.name}: {verb} (by {data.get(f'{verb}_by')})")
        for line in data.get("evidence", []):
            print(f"    | {line}")
        return 0

    if sub == "inspect":
        if api_up:
            result = await _common.api(
                boot, "GET", f"/api/agents/{args.name}/inspect", timeout=30.0
            )
            if result and result[0] == 200:
                data = result[1]
                if args.json:
                    _common.print_json(data)
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
                if data.get("detail"):
                    print(f"  detail:   {data['detail']}")
                print(f"  delivery: {data['delivery']}")
                if data.get("session_id"):
                    print(f"  session:  {data['session_id']}")
                if data.get("last_message"):
                    reply = " ".join(data["last_message"].split())
                    print(f"  last reply: {reply[:200]}{'…' if len(reply) > 200 else ''}")
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
        from agent_backbone.services.agents import agent_state
        from agent_backbone.services.terminal import session_exists

        config = await _common.load_config()
        online = await session_exists(args.name)
        snapshot = await agent_state(config, args.name)
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
            changes[key] = _common.parse_value(raw) if key in ("tags", "env", "always_on") else raw
        if api_up:
            result = await _common.api(boot, "PATCH", f"/api/agents/{args.name}", json_body=changes)
            if result and result[0] == 200:
                _common.print_json(result[1])
                return 0
            print(f"error: {result[1] if result else 'API unreachable'}")
            return 1
        async with _common.Direct(boot) as direct:
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
                result = await _common.api(
                    boot, "POST", f"/api/agents/{name}/{sub}", json_body={"repo": repo}
                )
                if not result or result[0] != 200:
                    print(f"error: {result[1] if result else 'API unreachable'}")
                    return 1
            else:
                async with _common.Direct(boot) as direct:
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
            result = await _common.api(boot, "DELETE", f"/api/agents/{args.name}")
            if result and result[0] == 200:
                print(f"{args.name}: forgotten")
                return 0
            print(f"error: {result[1] if result else 'API unreachable'}")
            return 1
        async with _common.Direct(boot) as direct:
            try:
                removed = await forget_agent(direct.store, args.name)
            except RuntimeError:
                print(
                    f"{args.name}: session is still running — "
                    f"stop it first (`backbone agent stop {args.name}`)"
                )
                return 1
        print(f"{args.name}: {'forgotten' if removed else 'unknown agent'}")
        return 0 if removed else 1

    return 1


def cmd_agent(args: argparse.Namespace) -> int:
    return asyncio.run(_agent(args))


async def _tell(args: argparse.Namespace) -> int:
    boot = await _common.client_config()
    text = " ".join(args.message)
    payload = {
        "target_session": args.agent,
        "from_entity": args.sender,
        "message": text,
        "priority": args.priority,
    }
    result = await _common.api(boot, "POST", "/api/messages", json_body=payload, timeout=30.0)
    if result is None:
        print("backbone API unreachable; is `backbone up` running?")
        return 1
    status, data = result
    if status != 200:
        print(f"error {status}: {data}")
        return 1
    print(json.dumps(data))
    if data.get("detail"):
        print(data["detail"])
    if data.get("ok"):
        return 0
    return 2 if data.get("queued") else 1


def cmd_tell(args: argparse.Namespace) -> int:
    return asyncio.run(_tell(args))


async def _reply(args: argparse.Namespace) -> int:
    boot = await _common.client_config()
    agent = args.agent or os.environ.get("BACKBONE_AGENT", "").strip()
    if not agent:
        print("no agent: pass --agent NAME (inside an agent session $BACKBONE_AGENT is used)")
        return 1
    payload = {"session": agent, "text": " ".join(args.text)}
    result = await _common.api(
        boot, "POST", "/api/integrations/reply", json_body=payload, timeout=30.0
    )
    if result is None:
        print("backbone API unreachable; is `backbone up` running?")
        return 1
    status, data = result
    if status != 200:
        detail = data.get("detail") if isinstance(data, dict) else data
        print(f"not posted: {detail}")
        return 1
    posted = ", ".join(name for name, ok in data.get("posted", {}).items() if ok)
    print(f"posted to {posted} as {agent}")
    return 0


def cmd_reply(args: argparse.Namespace) -> int:
    return asyncio.run(_reply(args))


def cmd_hooks(args: argparse.Namespace) -> int:
    """Hooks for sessions started *outside* the backbone (its own get them at launch)."""
    from agent_backbone.services.runtimes import get_runtime

    config = bootstrap_config()
    rt = get_runtime(args.runtime)
    project_dir = Path(args.dir).expanduser() if args.dir else None
    if args.hooks_command == "install":
        installed = rt.install_hooks(config.data_dir, config.state_dir, project_dir=project_dir)
        if installed is None:
            print(f"{rt.display_name} has no settings file the backbone edits")
            return 1
        settings_path, command = installed
        print(f"installed {rt.display_name} hooks in {settings_path}")
        print(f"  command: {command}")
        print(f"  state:   {config.state_dir}")
        print(f"Restart running {rt.display_name} sessions for the hooks to take effect.")
        if rt.id == "codex":
            print("Codex asks once to trust new hooks: run /hooks in a session and accept them.")
        return 0
    if args.hooks_command == "uninstall":
        settings_path = rt.uninstall_hooks(project_dir=project_dir)
        if settings_path is None:
            print(f"{rt.display_name} has no settings file the backbone edits")
            return 1
        print(f"removed agent-backbone hooks from {settings_path}")
        return 0
    return 1
