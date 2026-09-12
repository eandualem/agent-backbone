"""``backbone agent …``, ``tell``, ``reply`` and ``hooks`` — working with agents."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from agent_backbone.cli import _common
from agent_backbone.cli.presentation import note, print_record, print_table
from agent_backbone.config import (
    bootstrap_config,
)

log = logging.getLogger(__name__)


def _print_inspection(data: dict) -> None:
    note(
        f"{data['name']}: {'online' if data['online'] else 'offline'}"
        f"{'' if data['known'] else ' (not a known agent)'}"
    )
    print_record(
        "Identity",
        [
            ("Purpose", data.get("description") or "not described"),
            ("Tags", ", ".join(data.get("tags", [])) or "none"),
            ("Directory", data.get("dir")),
            ("Repository", data.get("repo")),
            ("Watches", ", ".join(data.get("watches", [])) or "none"),
            ("CLI", data.get("runtime")),
            ("Configured model", data.get("model") or "default"),
        ],
    )
    fields = [("State", data["state"]), ("Delivery", data["delivery"])]
    for key, label in (
        ("reason", "Reason"),
        ("detail", "Detail"),
        ("state_age_seconds", "Hook age (seconds)"),
        ("session_id", "Session"),
        ("last_message", "Last reply"),
    ):
        if data.get(key) is not None:
            fields.append((label, data[key]))
    if data.get("current_issue"):
        fields.append(
            ("Current issue", f"{data.get('current_repo') or ''}#{data['current_issue']}")
        )
    fields.append(("Evidence", "\n".join(data.get("evidence", []))))
    print_record("Observation", fields)
    if data.get("pane_tail"):
        note("Terminal tail", style="bold")
        note("\n".join(data["pane_tail"]))
    if data.get("recent_deliveries"):
        print_table(
            "Recent deliveries",
            ("Time", "Reference", "Outcome"),
            [
                (
                    d["created_at"],
                    f"{d.get('repo') or ''}#{d['issue_number']}"
                    if d.get("issue_number")
                    else d.get("kind"),
                    d["outcome"],
                )
                for d in data["recent_deliveries"]
            ],
        )


def _print_start_result(data: dict) -> None:
    name = data.get("name") or data.get("session")
    if data.get("already_existed"):
        print(f"{name}: already running")
        print(f"  open: backbone agent attach {name}")
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
    print(f"  open: backbone agent attach {name}")
    print(f"  details: backbone agent inspect {name}")
    for line in data.get("evidence", []):
        print(f"  - {line}")
    if ready in ("timeout", "waiting_for_human"):
        print(f"  answer it there: backbone agent attach {name}")


def always_on_names(config) -> list[str]:
    """The agents expected to stay up — what ``agent start --always-on`` brings back."""
    return [spec.name for spec in config.agents if spec.always_on]


async def _agent_start(args: argparse.Namespace) -> int:
    if getattr(args, "attach", False) and (
        len(args.names) > 1 or getattr(args, "always_on", False)
    ):
        print("--attach requires a single agent")
        return 1
    boot = await _common.read_client_config()
    if getattr(args, "always_on", False):
        if args.names or args.dir:
            print("--always-on selects the always_on agents itself; do not pass names or --dir")
            return 1
        names = always_on_names(await _common.read_config())
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
        config = await _common.read_config()
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
        args.attach_name = data.get("name") or data.get("session")
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
        args.attach_name = spec.name
        print(
            "note: the backbone is not running — start it with `backbone up --detach` for routing"
        )
        return 0 if result.ready != "exited" else 1


async def _agent(args: argparse.Namespace) -> int:
    from agent_backbone.services.agents.operations import forget_agent, stop_agent_session

    sub = args.agent_command
    if sub in ("start", "resume"):
        return await _agent_start(args)

    boot = await _common.read_client_config()
    api_up = await _common.api_up(boot)

    if sub == "list":
        config = await _common.read_config()
        specs = [spec for spec in config.agents if not args.tag or args.tag in spec.tags]
        if args.json:
            from agent_backbone.services.agents import AgentConfigView

            _common.print_json(
                {"items": [AgentConfigView.from_spec(s).model_dump() for s in specs]}
            )
            return 0
        if not specs:
            print("No agents known yet. Run `backbone agent start` from a project directory.")
            return 0
        print_table(
            "Agents",
            ("Agent", "Purpose", "Tags", "CLI / Model"),
            [
                (
                    spec.name,
                    spec.description or "not described",
                    "\n".join(spec.tags) or "none",
                    f"{spec.runtime} / {spec.model or 'default'}",
                )
                for spec in sorted(specs, key=lambda spec: spec.name.casefold())
            ],
        )
        note("Live state: backbone status\nFull details and location: backbone agent inspect NAME")
        return 0

    if sub in ("tag", "untag", "rename"):
        body = (
            {"name": args.new_name}
            if sub == "rename"
            else {"tags": args.tags, "remove": sub == "untag"}
        )
        if api_up:
            endpoint = "rename" if sub == "rename" else "tags"
            response = await _common.api(
                boot, "POST", f"/api/agents/{args.name}/{endpoint}", json_body=body
            )
            if not response or response[0] != 200:
                print(f"error: {response[1] if response else 'API unreachable'}")
                return 1
        else:
            async with _common.Direct(boot) as direct:
                try:
                    if sub == "rename":
                        await direct.store.rename(args.name, args.new_name)
                    else:
                        await direct.store.tag(args.name, args.tags, remove=sub == "untag")
                except (KeyError, ValueError, OSError) as exc:
                    print(f"error: {exc}")
                    return 1
        if sub == "rename":
            print(f"{args.name} renamed to {args.new_name}")
            print(f"Resume: backbone agent resume {args.new_name} --attach")
            print(f"Update external for:{args.name} labels and scripts to use {args.new_name}.")
        else:
            print(f"{args.name}: tags updated")
        return 0

    if sub == "stop":
        if not api_up:
            boot = await _common.read_config()
        failed = False
        for name in args.names:
            if not api_up and name == boot.backbone.session_name:
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
                _print_inspection(data)
                return 0
            print(f"error: {result[1] if result else 'API unreachable'}")
            return 1
        # Offline inspection: state file + tmux only.
        from agent_backbone.services.agents import agent_state
        from agent_backbone.services.terminal import session_exists

        config = await _common.read_config()
        online = await session_exists(args.name)
        snapshot = await agent_state(config, args.name)
        spec = config.agents.get(args.name)
        if args.json:
            from dataclasses import asdict

            spec = config.agents.get(args.name)
            _common.print_json(
                {
                    **asdict(snapshot),
                    "name": args.name,
                    "online": online,
                    "known": spec is not None,
                    "state": snapshot.state.value if online else "offline",
                    "dir": str(spec.path) if spec else "",
                    "description": spec.description if spec else "",
                    "tags": list(spec.tags) if spec else [],
                    "model": spec.model if spec else None,
                    "runtime": snapshot.runtime or (spec.runtime if spec else None),
                    "repo": spec.repo if spec else "",
                    "watches": list(spec.watches) if spec else [],
                }
            )
            return 0
        note(f"{args.name}: {'online' if online else 'offline'} (backbone not running)")
        print_record(
            "Identity",
            [
                ("Purpose", (spec.description if spec else "") or "not described"),
                ("Tags", (", ".join(spec.tags) if spec else "") or "none"),
                ("Directory", str(spec.path) if spec else "-"),
                ("Repository", spec.repo if spec else "-"),
                ("CLI", snapshot.runtime or (spec.runtime if spec else "unknown")),
                ("Configured model", (spec.model if spec else None) or "default"),
            ],
        )
        print_record(
            "Observation",
            [
                ("State", snapshot.state.value if online else "offline"),
                ("Reason", snapshot.reason),
                ("Evidence", "\n".join(snapshot.evidence)),
            ],
        )
        return 0

    if sub == "set":
        changes: dict[str, Any] = {}
        for item in args.assignments:
            if "=" not in item:
                print(f"expected key=value, got {item!r}")
                return 1
            key, raw = item.split("=", 1)
            json_keys = ("tags", "env", "always_on", "unattended")
            changes[key] = _common.parse_value(raw) if key in json_keys else raw
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
    attach = args.agent_command == "attach" or getattr(args, "attach", False)
    if attach and not sys.stdin.isatty():
        print("attachment needs an interactive terminal; use `backbone agent inspect NAME`")
        return 1
    try:
        result = 0 if args.agent_command == "attach" else asyncio.run(_agent(args))
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    if result == 0 and attach:
        from agent_backbone.services.terminal import attach_session

        name = args.name if args.agent_command == "attach" else args.attach_name
        try:
            return attach_session(name, read_only=getattr(args, "read_only", False))
        except (OSError, ValueError) as exc:
            print(f"could not attach to {name}: {exc}")
            return 1
    return result


async def _tell(args: argparse.Namespace) -> int:
    boot = await _common.read_client_config()
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
    if not isinstance(data, dict):
        # A 200 whose body is not JSON-object (a proxy page massaged by the
        # API client) has no outcome to report — tracebacking on .get helps
        # nobody.
        print(f"error: unexpected response from the backbone API: {data!r}"[:500])
        return 1
    print(json.dumps(data))
    if data.get("detail"):
        print(data["detail"])
    if data.get("ok"):
        return 0
    if data.get("operation_id"):
        print(f"trace: backbone diagnostics trace {data['operation_id']}")
    return 2 if data.get("queued") else 1


def cmd_tell(args: argparse.Namespace) -> int:
    return asyncio.run(_tell(args))


async def _reply(args: argparse.Namespace) -> int:
    boot = await _common.read_client_config()
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
    if not isinstance(data, dict):
        print(f"error: unexpected response from the backbone API: {data!r}"[:500])
        return 1
    if not data.get("ok"):
        # The server fails posts as nonzero (502/404/503), so a 200
        # reporting ok:false is a foreign/proxied body, not a server
        # failure mode — still a failure to the user, never a blank
        # "posted to " with exit 0.
        print(f"not posted: {data}")
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


async def _inbox(args: argparse.Namespace) -> int:
    if not args.agent:
        print("Use --agent NAME or run inside an agent session ($BACKBONE_AGENT)")
        return 1
    boot = await _common.read_client_config()
    result = await _common.api(
        boot,
        "POST",
        "/api/messages/inbox",
        json_body={
            "session": args.agent,
            "acknowledge": args.ack,
        },
    )
    if result is None:
        print("backbone API unreachable; is `backbone up` running?")
        return 1
    status, data = result
    print(json.dumps(data))
    return 0 if status == 200 else 1


def cmd_inbox(args: argparse.Namespace) -> int:
    return asyncio.run(_inbox(args))
