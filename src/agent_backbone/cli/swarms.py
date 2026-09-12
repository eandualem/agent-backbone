"""``backbone swarm …`` and ``help`` — swarms and the agent-facing help topics."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

from agent_backbone.cli import _common
from agent_backbone.cli.presentation import note, print_record, print_table
from agent_backbone.config import (
    bootstrap_config,
)

log = logging.getLogger(__name__)


async def _swarm(args: argparse.Namespace) -> int:
    boot = await _common.read_client_config()
    sub = args.swarm_command

    if sub == "create":
        initiator = args.initiator or os.environ.get("BACKBONE_AGENT", "").strip()
        body = {
            "name": args.name,
            "issue": args.issue,
            "members": args.member or [],
            "initiator": initiator,
        }
        result = await _common.api(boot, "POST", "/api/swarms", json_body=body, timeout=300.0)
        if result is None:
            print("backbone API unreachable; `backbone up` must be running to create a swarm")
            return 1
        status, data = result
        if status != 200:
            print(f"error: {data.get('detail') if isinstance(data, dict) else data}")
            return 1
        print(f"swarm '{data['name']}' is live on {data['repo']}#{data['issue_number']}")
        print(f"  coordinator: {data['coordinator']}")
        print(f'  talk to it:  backbone tell {data["name"]} "..."')
        print(f"  members:     {', '.join(data['members'])}")
        print(f"  branch:      {data['branch']}")
        print(f"  worktree:    {data['worktree']}")
        return 0

    if sub == "list":
        result = await _common.api(boot, "GET", "/api/swarms")
        if result is None or result[0] != 200:
            print("backbone API unreachable")
            return 1
        swarms = result[1].get("items", []) if isinstance(result[1], dict) else []
        if not isinstance(swarms, list) or any(not isinstance(s, dict) for s in swarms):
            # A 200 with a body that is not a swarm list (a proxy, a version
            # skew) is an error, not an empty swarm list — and must not reach
            # the direct indexes below as a traceback.
            print("error: unexpected swarm list from the backbone API")
            return 1
        if not swarms:
            print("no swarms")
            return 0
        for swarm in swarms:
            print_record(
                f"Swarm {swarm.get('name', '?')}",
                [
                    ("State", swarm.get("status", "unknown")),
                    ("Issue", f"{swarm.get('repo', '?')}#{swarm.get('issue_number', '?')}"),
                    ("Branch", swarm.get("branch", "?")),
                ],
            )
            members = swarm.get("members", [])
            rows = []
            for member in members if isinstance(members, list) else []:
                if not isinstance(member, dict):
                    continue
                state = member.get("state", "unknown")
                if member.get("reason"):
                    state += f" ({member['reason']})"
                rows.append(
                    (
                        member.get("name", "?"),
                        member.get("role", "?"),
                        f"{member.get('runtime', '?')} / {member.get('model') or 'default'}",
                        state,
                        member.get("detail") or "-",
                    )
                )
            print_table("Members", ("Agent", "Role", "CLI / Model", "State", "Detail"), rows)
        note("Live state: backbone swarm status NAME\nDetails: backbone agent inspect NAME")
        return 0

    if sub == "disband":
        result = await _common.api(boot, "DELETE", f"/api/swarms/{args.name}", timeout=60.0)
        if result is None:
            print("backbone API unreachable")
            return 1
        status, data = result
        if status != 200:
            print(f"error: {data.get('detail') if isinstance(data, dict) else data}")
            return 1
        print(f"swarm '{args.name}': {data['status']}")
        return 0
    return 1


def cmd_swarm(args: argparse.Namespace) -> int:
    if args.swarm_command == "status":
        from agent_backbone.cli.status import cmd_status

        args.swarm = args.name or args.swarm
        args.swarms_only = True
        return cmd_status(args)
    return asyncio.run(_swarm(args))


def cmd_help(args: argparse.Namespace) -> int:
    """Agent-facing capability help, straight from the installed package."""
    from agent_backbone.help import get_topic, list_topics

    data_dir = bootstrap_config().data_dir
    if args.topic == "usage" and not getattr(args, "path", []):
        args.page = "usage"
        return cmd_docs(args)
    if args.topic and not getattr(args, "path", []):
        content = get_topic(args.topic, data_dir)
        if content is not None:
            print(content)
            return 0
    if args.topic:
        from agent_backbone.cli import build_parser

        parser = build_parser()
        commands = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        if args.topic in commands.choices:
            parser = commands.choices[args.topic]
            for name in getattr(args, "path", []):
                commands = next(
                    (a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None
                )
                if commands is None or name not in commands.choices:
                    print(f"unknown subcommand '{name}'")
                    return 1
                parser = commands.choices[name]
            parser.print_help()
            return 0
    if not args.topic:
        print("backbone capabilities — `backbone help <topic>` for the details:\n")
        print_table(
            "Capabilities",
            ("Topic", "Purpose"),
            [(topic["name"], topic["summary"]) for topic in list_topics(data_dir)],
        )
        return 0
    content = get_topic(args.topic, data_dir)
    if content is None:
        known = ", ".join(t["name"] for t in list_topics(data_dir))
        print(f"unknown topic '{args.topic}' — try: {known}")
        return 1
    print(content)
    return 0


def cmd_docs(args: argparse.Namespace) -> int:
    """The user documentation, straight from the installed package."""
    from agent_backbone.help import get_doc, list_docs

    pages = list_docs()
    if not pages:
        print("no documentation shipped with this install — read it at")
        print("https://github.com/eandualem/agent-backbone/tree/main/docs")
        return 1
    if not args.page:
        print("agent-backbone documentation — `backbone docs <page>` prints one page:\n")
        print_table(
            "Documentation",
            ("Page", "Purpose"),
            [(page["name"], page["summary"]) for page in pages],
        )
        return 0
    content = get_doc(args.page)
    if content is None:
        known = ", ".join(p["name"] for p in pages)
        print(f"unknown page '{args.page}' — try: {known}")
        return 1
    print(content)
    return 0
