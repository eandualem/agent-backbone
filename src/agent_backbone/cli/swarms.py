"""``backbone swarm …`` and ``help`` — swarms and the agent-facing help topics."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

from agent_backbone.cli import _common
from agent_backbone.config import (
    bootstrap_config,
)

log = logging.getLogger(__name__)


async def _swarm(args: argparse.Namespace) -> int:
    boot = await _common.client_config()
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

    if sub in ("list", "status"):
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
        if sub == "status" and getattr(args, "name", None):
            swarms = [s for s in swarms if s.get("name") == args.name]
            if not swarms:
                print(f"unknown swarm '{args.name}'")
                return 1
        if not swarms:
            print("no swarms")
            return 0
        for swarm in swarms:
            print(
                f"{swarm.get('name', '?'):<16s} {swarm.get('status', '?'):<10s} "
                f"{swarm.get('repo', '?')}#{swarm.get('issue_number', '?')}  "
                f"branch {swarm.get('branch', '?')}"
            )
            members = swarm.get("members", [])
            for member in members if isinstance(members, list) else []:
                if not isinstance(member, dict):
                    continue
                model = f" ({member['model']})" if member.get("model") else ""
                print(
                    f"    {member.get('name', '?'):<28s} {member.get('role', '?'):<12s} "
                    f"{member.get('runtime', '?')}{model}"
                )
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
    return asyncio.run(_swarm(args))


def cmd_help(args: argparse.Namespace) -> int:
    """Agent-facing capability help, straight from the installed package."""
    from agent_backbone.help import get_topic, list_topics

    data_dir = bootstrap_config().data_dir
    if not args.topic:
        print("backbone capabilities — `backbone help <topic>` for the details:\n")
        for topic in list_topics(data_dir):
            print(f"  {topic['name']:<12s} {topic['summary']}")
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
        for page in pages:
            print(f"  {page['name']:<20s} {page['summary']}")
        return 0
    content = get_doc(args.page)
    if content is None:
        known = ", ".join(p["name"] for p in pages)
        print(f"unknown page '{args.page}' — try: {known}")
        return 1
    print(content)
    return 0
