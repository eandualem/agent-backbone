"""A compact, TTY-aware view of the existing session feed."""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
import time
from collections import Counter
from io import StringIO

from rich import box
from rich.cells import cell_len
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

from agent_backbone.cli import _common


def add_status_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="machine-readable snapshot")
    parser.add_argument("--plain", action="store_true", help="ASCII borders and no color")
    parser.add_argument("--watch", action="store_true", help="refresh until Ctrl-C")
    parser.add_argument("--interval", type=float, default=3, metavar="SECONDS")
    parser.add_argument("--tag", help="show agents with this tag")
    parser.add_argument("--swarm", help="show members of one swarm")
    parser.add_argument("--running", action="store_true", help="hide stopped agents")
    parser.add_argument(
        "--details", action="store_true", help="include last replies and activity times"
    )


def clean(value: object) -> str:
    """One printable line; remote text must not inject terminal escape sequences."""
    return "".join(
        " " if c.isspace() and c != " " else c
        for c in str(value or "")
        if c.isprintable() or c.isspace()
    )


def age(timestamp: object, now: float) -> str:
    try:
        seconds = max(0, int(now - float(timestamp)))
    except (ValueError, TypeError, OverflowError):
        return ""
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


# Display order only: state and its evidence still come from the session feed.
_STATES = {
    "waiting_for_human": (0, "waiting", "bold yellow"),
    "blocked": (1, "blocked", "bold red"),
    "busy": (2, "busy", "cyan"),
    "starting": (3, "starting", "yellow"),
    "idle": (4, "idle", "green"),
    "unknown": (5, "unknown", "yellow"),
    "offline": (6, "offline", "dim"),
}


def _state(agent: dict) -> tuple[int, str, str]:
    state = clean(agent.get("state", "unknown"))
    return _STATES.get(state, (5, state, "yellow"))


def _work(agent: dict, *, compact: bool) -> str:
    repo = clean(agent.get("current_repo") or agent.get("repo"))
    if compact:
        repo = repo.rsplit("/", 1)[-1]
    if agent.get("current_issue"):
        issue = f"{repo}#{clean(agent['current_issue'])}"
        return issue if agent.get("online") else f"Last: {issue}"
    return repo or clean(agent.get("description")) or "—"


def status_view(data: dict, *, width: int, plain: bool = False, watch: bool = False) -> Group:
    """Build literal Rich text, never interpreting agent/provider text as markup."""
    width = max(20, width)
    agents = data["agents"]
    counts = Counter(a.get("state", "unknown") for a in agents)
    heading = Text("BACKBONE", style="bold")
    heading.append("  •  " if not plain else "  /  ", style="dim")
    heading.append(
        "connected" if data["api_online"] else "offline · local sessions",
        style="green" if data["api_online"] else "yellow",
    )
    if watch:
        heading.append("  |  Ctrl-C to exit", "dim")
    summary = Text(f"{len(agents)} {'agent' if len(agents) == 1 else 'agents'}", style="bold")
    for state in sorted(counts, key=lambda s: _state({"state": s})[0]):
        _, label, style = _state({"state": state})
        summary.append("  ·  " if not plain else "  |  ", style="dim")
        summary.append(f"{counts[state]} {label}", style=style)
    parts = [heading, summary]
    if data.get("unhealthy"):
        parts.append(Text("Needs attention: " + clean(", ".join(data["unhealthy"])), "bold red"))
    if not agents:
        parts.append(
            Text(
                "No agents match." if data.get("filtered") else "Start here: backbone agent start",
                "dim",
            )
        )

    groups: dict[str | None, list[dict]] = {}
    for agent in agents:
        swarm = next((t[6:] for t in agent.get("tags", []) if t.startswith("swarm:")), None)
        groups.setdefault(swarm, []).append(agent)
    swarm_info = {s["name"]: s for s in data.get("swarms", [])}
    # Include swarms whose member sessions have gone away.
    for name in swarm_info:
        groups.setdefault(name, [])
    for group, members in sorted(
        groups.items(), key=lambda item: (item[0] is not None, item[0] or "")
    ):
        title = Text("Agents" if group is None else f"Swarm / {clean(group)}", "bold")
        if swarm := swarm_info.get(group):
            title.append(
                f"  {clean(swarm['repo'])}#{swarm['issue_number']} · {clean(swarm['status'])}",
                "dim",
            )
        table = Table(
            title=title,
            title_justify="left",
            box=box.ASCII if plain else box.ROUNDED,
            border_style="dim",
            header_style="bold cyan",
            expand=True,
            padding=(0, 1),
            show_edge=True,
        )
        # Keep the roster a table at every width. On small terminals fold the
        # runtime/model columns together, then omit them; --details has full text.
        if width >= 80:
            name_width = min(
                max(5, max((cell_len(clean(a["name"])) for a in members), default=12)),
                18 if width < 100 else 24,
            )
            state_width = 8
            runtime_width = 6
            model_width = min(24, 12 + (width - 80) // 2)
            columns = [
                ("Agent", name_width),
                ("State", state_width),
                ("CLI", runtime_width),
                ("Model", model_width),
                ("Work", width - 16 - name_width - state_width - runtime_width - model_width),
            ]
        elif width >= 60:
            columns = [("Agent", 14), ("State", 8), ("CLI / Model", 13), ("Work", width - 48)]
        else:
            name_width = max(4, min(14, (width - 18) // 2))
            columns = [
                ("Agent", name_width),
                ("State", 7),
                ("Work", max(1, width - 17 - name_width)),
            ]
        for label, size in columns:
            table.add_column(label, width=size, no_wrap=True, overflow="ellipsis")
        ordered = sorted(members, key=lambda a: (_state(a)[0], clean(a["name"]).casefold()))
        for index, agent in enumerate(ordered):
            _, label, style = _state(agent)
            cells = [
                Text(clean(agent["name"]), "bold" if agent.get("online") else "dim"),
                Text(label, style),
            ]
            runtime = clean(agent.get("runtime") or "unknown")
            model = clean(agent.get("model") or "default")
            if width >= 80:
                cells.extend([Text(runtime), Text(model)])
            elif width >= 60:
                cells.append(Text(f"{runtime} / {model}"))
            cells.append(Text(_work(agent, compact=width < 100)))
            # A single divider separates live work from stopped sessions.
            end_section = (
                agent.get("state") != "offline"
                and index + 1 < len(ordered)
                and ordered[index + 1].get("state") == "offline"
            )
            table.add_row(
                *cells,
                style="dim" if agent.get("state") == "offline" else "",
                end_section=end_section,
            )
        parts.extend([Text(), table])

    details = Table(box=None, padding=(0, 1), show_header=False, expand=True)
    details.add_column(width=min(18, max(5, width // 4)), style="bold", overflow="fold")
    details.add_column(ratio=1, overflow="fold")
    for agent in sorted(agents, key=lambda a: (_state(a)[0], clean(a["name"]).casefold())):
        information = []
        if data.get("details"):
            information.append(
                f"{clean(agent.get('runtime') or 'unknown')} / "
                f"{clean(agent.get('model') or 'default')}"
            )
            if work := _work(agent, compact=False):
                information.append(work)
            if agent.get("tags"):
                information.append("Tags: " + clean(", ".join(agent["tags"])))
        if reason := agent.get("reason"):
            information.append("Reason: " + clean(reason))
        for key, label in (
            ("detail", "Detail"),
            ("plan_title", "Plan"),
            ("last_message", "Last reply"),
        ):
            if agent.get(key) and (key != "last_message" or data.get("details")):
                information.append(f"{label}: {clean(agent[key])}")
        if data.get("details"):
            elapsed = age(agent.get("last_activity"), data.get("captured_at", time.time()))
            if elapsed:
                information.append(
                    f"Terminal activity {elapsed}"
                    + (" · attached" if agent.get("tmux_attached") else "")
                )
        if information:
            details.add_row(Text(clean(agent["name"])), Text("\n".join(information)))
    if details.row_count:
        parts.extend([Text(), Text("Details", "bold"), details])
    if not watch:
        parts.extend(
            [
                Text(),
                Text("Attach   backbone agent attach NAME", "dim"),
                Text("Inspect  backbone agent inspect NAME", "dim"),
                Text("Watch    backbone status --watch", "dim"),
            ]
        )
    return Group(*parts)


def render_status(data: dict, *, width: int = 100, color: bool = False, plain: bool = False) -> str:
    output = StringIO()
    console = Console(
        file=output,
        width=max(20, width),
        force_terminal=color,
        color_system="standard" if color and not plain else None,
        markup=False,
        highlight=False,
    )
    console.print(status_view(data, width=console.width, plain=plain))
    return output.getvalue().rstrip("\n")


async def snapshot(args: argparse.Namespace) -> dict:
    config = await _common.read_client_config()
    health, response, swarm_response = await asyncio.gather(
        _common.api(config, "GET", "/health", timeout=3),
        _common.api(config, "GET", "/api/agents", timeout=5),
        _common.api(config, "GET", "/api/swarms", timeout=5),
    )
    online = response is not None
    if response is not None:
        status, body = response
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("items"), list):
            raise ValueError(f"cannot read agents from the API (HTTP {status})")
        agents = body["items"]
        if not swarm_response or swarm_response[0] != 200:
            raise ValueError("cannot read swarms from the API")
        swarm_body = swarm_response[1]
        if not isinstance(swarm_body, dict) or not isinstance(swarm_body.get("items"), list):
            raise ValueError("unexpected swarm list from the API")
        swarms = swarm_body["items"]
    else:
        from agent_backbone.api.session_updates import build_session_snapshot

        async with _common.Direct(config) as direct:
            agents = [
                a.model_dump(mode="json") for a in await build_session_snapshot(direct.config)
            ]
            swarms = await direct.db.swarms.list()
    if args.swarm and not any(s["name"] == args.swarm for s in swarms):
        raise ValueError(f"unknown swarm '{args.swarm}'")
    agents = [
        a
        for a in agents
        if (not args.running or a.get("online"))
        and (not args.tag or args.tag in a.get("tags", []))
        and (not args.swarm or f"swarm:{args.swarm}" in a.get("tags", []))
        and (
            not getattr(args, "swarms_only", False)
            or any(t.startswith("swarm:") for t in a.get("tags", []))
        )
    ]
    visible_swarms = {t[6:] for a in agents for t in a.get("tags", []) if t.startswith("swarm:")}
    swarms = [
        s
        for s in swarms
        if (
            s["name"] == args.swarm
            if args.swarm
            else (
                s["name"] in visible_swarms if args.tag or args.running else s["status"] == "active"
            )
        )
    ]
    components = health[1].get("components", {}) if health and isinstance(health[1], dict) else {}
    return {
        "api_online": online,
        "captured_at": time.time(),
        "agents": agents,
        "swarms": swarms,
        "filtered": bool(args.tag or args.swarm or args.running),
        "details": args.details,
        "unhealthy": [n for n, c in components.items() if not c.get("healthy")],
    }


async def _status(args: argparse.Namespace) -> int:
    if not math.isfinite(args.interval) or args.interval < 1:
        print("--interval must be at least 1 second", file=sys.stderr)
        return 1
    if args.watch and args.json:
        print("--json returns one snapshot; omit --watch", file=sys.stderr)
        return 1
    if args.watch and not sys.stdout.isatty():
        print("--watch needs a terminal; use --json for scripts", file=sys.stderr)
        return 1
    if args.watch and os.environ.get("TERM") == "dumb":
        print("--watch needs a terminal with cursor support; omit --watch", file=sys.stderr)
        return 1
    console = Console(color_system=None if args.plain else "auto", markup=False, highlight=False)
    try:
        data = await snapshot(args)
        if args.json:
            _common.print_json(data)
        elif not args.watch:
            console.print(status_view(data, width=console.width, plain=args.plain))
        else:
            with Live(
                console=console, screen=True, auto_refresh=False, vertical_overflow="ellipsis"
            ) as live:
                while True:
                    live.update(
                        status_view(data, width=console.width, plain=args.plain, watch=True),
                        refresh=True,
                    )
                    await asyncio.sleep(args.interval)
                    data = await snapshot(args)
        return 0
    except (OSError, ValueError) as exc:
        print(f"status: {clean(exc)}", file=sys.stderr)
        return 1


def cmd_status(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(_status(args))
    except KeyboardInterrupt:
        return 0
