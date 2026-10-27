"""Read artifact conversations and reply in them as your own registration."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from urllib.parse import quote, urlencode

from agent_backbone.cli import _common
from agent_backbone.cli.presentation import print_record, print_table


def _positive_id(value: str) -> int:
    try:
        number = int(value)
        if 1 <= number <= 2**63 - 1:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("entry ID must be a positive integer")


def add_conversation_parsers(subparsers) -> None:
    read = subparsers.add_parser(
        "conversations", help="read and reply in the conversations on artifacts"
    )
    read.add_argument("--kind", choices=("document", "response", "report"))
    read.add_argument("--limit", type=int, choices=range(1, 201), metavar="1–200", default=50)
    read.add_argument("--agent", help="read as this agent (default: $BACKBONE_AGENT)")
    read.add_argument("--json", action="store_true")
    sub = read.add_subparsers(dest="conversation_command")
    show = sub.add_parser("show", help="read one conversation, newest entries last")
    show.add_argument("kind")
    show.add_argument("id")
    show.add_argument("--before", type=_positive_id, help="read entries older than this ID")
    show.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    reply = sub.add_parser("reply", help="post a message in a conversation")
    reply.add_argument("kind")
    reply.add_argument("id")
    reply.add_argument("text")
    reply.add_argument("--to", type=_positive_id, help="the entry you are answering")
    reply.add_argument("--revision", help="the version of the artifact this reply produced")
    reply.add_argument("--anchor", help="the version you were looking at")
    reply.add_argument("--key", help="retry key (default: hash of the reply)")
    reply.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    read.set_defaults(func=lambda args: asyncio.run(_run(args)))


def _error(args, detail, code=1) -> int:
    if args.json:
        _common.print_json({"error": "conversation_error", "detail": detail})
    else:
        print_record("Conversation error", [("Detail", detail)])
    return code


def _path(args) -> str:
    return f"/api/conversations/{quote(args.kind, safe='')}/{quote(args.id, safe='')}"


async def _run(args) -> int:
    agent = args.agent or os.environ.get("BACKBONE_AGENT", "").strip() or None
    command = args.conversation_command
    if command == "reply":
        if not agent:
            return _error(args, "Pass --agent NAME or run inside your agent session.", 2)
        body = {
            "author": agent,
            "text": args.text,
            "in_reply_to": args.to,
            "resulting_revision_id": args.revision,
            "anchor": {"revision_id": args.anchor} if args.anchor else None,
        }
        digest = hashlib.sha256(
            json.dumps([args.kind, args.id, body], sort_keys=True).encode()
        ).hexdigest()
        body["request_id"] = args.key or digest
        method, path = "POST", _path(args) + "/messages"
    else:
        params = {"limit": args.limit} | ({"agent": agent} if agent else {})
        if command == "show":
            path = _path(args)
            if args.before:
                params["before"] = args.before
        else:
            path = "/api/conversations"
            if args.kind:
                params["kind"] = args.kind
        method, body, path = "GET", None, path + "?" + urlencode(params)
    result = await _common.api(await _common.read_client_config(), method, path, json_body=body)
    if result is None:
        return _error(args, "Backbone is unreachable. Retry the same reply when it is back.")
    status, data = result
    if status not in {200, 201}:
        return _error(args, data.get("message") or data.get("detail") or f"HTTP {status}")
    if args.json:
        _common.print_json(data)
    elif command == "reply":
        entry = data["entry"]
        verb = "Posted" if status == 201 else "Already posted"
        print(f"{verb} entry {entry['id']} in {args.kind} {args.id}.")
    elif command == "show":
        _print_conversation(data)
    else:
        print_table(
            "Conversations",
            ("Artifact", "Responsible", "Messages", "Unread", "Last entry"),
            (
                (
                    f"{item['kind']} {item['artifact_id']}",
                    item["responsible"]["name"] or item["responsible"]["status"],
                    item["total"],
                    item["unread"],
                    item["last_entry_at"],
                )
                for item in data["items"]
            ),
            empty="No conversations.",
        )
    return 0


def _print_conversation(data) -> None:
    responsible = data["responsible"]
    print_record(
        f"{data['kind'].capitalize()} {data['artifact_id']}",
        [
            ("Title", data["title"]),
            ("Latest version", data["latest_revision_id"]),
            ("Responsible", f"{responsible['name']} ({responsible['status']})"),
            ("Messages", data["total"]),
            ("Older entries", f"--before {data['older'][1:]}" if data["older"] else "none"),
        ],
    )
    rows = []
    for entry in data["entries"]:
        if entry["kind"] == "message":
            text = entry["text"]
            if entry.get("in_reply_to"):
                text = f"(reply to {entry['in_reply_to']}) {text}"
            if entry.get("resulting_revision_id"):
                text += f" (produced version {entry['resulting_revision_id']})"
            state = entry.get("delivery") or ""
        else:
            event = entry[entry["kind"]]
            text = f"published {event['label']} (version {event['revision_id']})"
            state = ""
        rows.append((entry["id"], entry["author"]["name"], text, state))
    print_table("Entries", ("Entry", "From", "Message", "Delivery"), rows, empty="No entries.")
