"""Author a short current checklist through the shared authenticated API."""

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlencode

from pydantic import ValidationError

from agent_backbone.cli import _common
from agent_backbone.cli.presentation import print_record, print_table
from agent_backbone.models import (
    CURRENT_PLAN_BODY_BYTES,
    CurrentPlanFeedback,
    PublishCurrentPlan,
    report_error_details,
)


def _identity(value):
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise argparse.ArgumentTypeError("ID must be 32 lowercase hexadecimal characters")
    return value


def add_current_plan_parsers(sub):
    root = sub.add_parser("current-plan", help="read or update an agent's current checklist")
    root.set_defaults(func=cmd_current_plan)
    root.add_argument("--agent", help="filter by current registered name")
    root.add_argument("--limit", type=int, choices=range(1, 101), default=50, metavar="1-100")
    root.add_argument("--after", type=_identity, help="next page's author ID")
    root.add_argument("--json", action="store_true")
    commands = root.add_subparsers(dest="current_plan_command")
    for name in ("show", "publish", "feedback"):
        command = commands.add_parser(name)
        command.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        if name != "publish":
            command.add_argument("author_id", type=_identity)
        if name != "show":
            source = command.add_mutually_exclusive_group(required=True)
            source.add_argument("--file", help="complete request JSON file, or - for stdin")
            source.add_argument("--schema", action="store_true")
            command.add_argument(
                "--validate", action="store_true", help="validate without publishing"
            )


def _error(args, detail, code=1):
    if args.json:
        _common.print_json({"error": "current_plan_error", "detail": detail})
    else:
        print(json.dumps(detail) if not isinstance(detail, str) else detail, file=sys.stderr)
    return code


def cmd_current_plan(args):
    payload = None
    command = args.current_plan_command
    if command in {"publish", "feedback"}:
        model = PublishCurrentPlan if command == "publish" else CurrentPlanFeedback
        if args.schema:
            _common.print_json(model.model_json_schema())
            return 0
        try:
            if args.file == "-":
                raw = getattr(sys.stdin, "buffer", sys.stdin).read(CURRENT_PLAN_BODY_BYTES + 1)
                if isinstance(raw, str):
                    raw = raw.encode()
            else:
                with Path(args.file).open("rb") as source:
                    raw = source.read(CURRENT_PLAN_BODY_BYTES + 1)
            if len(raw) > CURRENT_PLAN_BODY_BYTES:
                raise ValueError("current plan request exceeds 65536 UTF-8 bytes")
            payload = model.model_validate_json(raw).model_dump()
        except ValidationError as exc:
            return _error(args, report_error_details(exc.errors()), 2)
        except (OSError, ValueError) as exc:
            return _error(args, str(exc), 2)
        if args.validate:
            _common.print_json({"valid": True, "scope": "request shape; no publication"})
            return 0
    return asyncio.run(_run(args, payload))


async def _run(args, payload):
    command = args.current_plan_command
    path = "/api/current-plans"
    if command in {"show", "feedback"}:
        path += "/" + args.author_id
    if command == "feedback":
        path += "/feedback"
    if command is None:
        params = {"limit": args.limit}
        if args.agent:
            params["agent"] = args.agent
        if args.after:
            params["after_author_id"] = args.after
        path += "?" + urlencode(params)
    result = await _common.api(
        await _common.read_client_config(),
        "POST" if payload is not None else "GET",
        path,
        json_body=payload,
    )
    if result is None:
        return _error(
            args,
            "Backbone is unreachable. Preserve and retry the identical write request/key; "
            "acceptance may be unknown.",
        )
    status, data = result
    if status not in {200, 201}:
        return _error(args, data.get("detail", f"HTTP {status}"))
    if args.json:
        _common.print_json(data)
    elif command == "publish":
        print(
            ("Published" if data["created"] else "Already published")
            + " current plan revision "
            + data["receipt"]["revision_id"]
        )
    elif command == "feedback":
        print("Feedback " + data["feedback_id"] + ": " + data["delivery"]["detail"])
    elif command == "show":
        fields = [
            ("Agent", data["agent_name"]),
            ("Registration", data["author_id"]),
            ("Plan", data["plan_id"]),
            ("Revision", data["revision_id"]),
            ("Updated", data["updated_at"]),
            ("Freshness", "stale" if data["stale"] else "current"),
        ]
        for item in data["plan"]["items"]:
            refs = ", ".join(r["kind"] + ":" + r["target"] for r in item["references"])
            fields.append(
                (
                    item["section"].title(),
                    f"[{item['status']}] {item['text']}\nID: {item['item_id']}\n"
                    f"{item['detail']}\n{refs}",
                )
            )
        if not data["plan"]["items"]:
            fields.append(("Work", "No current items."))
        print_record(data["plan"]["title"], fields)
    else:
        print_table(
            "Current plans",
            ("Agent", "Registration", "Plan", "Updated", "Freshness"),
            [
                (
                    x["agent_name"],
                    x["author_id"],
                    x["record"]["plan"]["title"] if x["record"] else "Not published",
                    x["record"]["updated_at"] if x["record"] else "Not set",
                    ("stale" if x["record"]["stale"] else "current")
                    if x["record"]
                    else "Not published",
                )
                for x in data["items"]
            ],
        )
        if data["next_after_author_id"]:
            print("Next page: repeat with --after " + data["next_after_author_id"])
    return 0
