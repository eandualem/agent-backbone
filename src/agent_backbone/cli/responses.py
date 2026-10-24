"""Publish and retrieve explicit answers without interrupting agent work."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlencode

from pydantic import ValidationError

from agent_backbone.cli import _common
from agent_backbone.cli.presentation import print_record
from agent_backbone.models import (
    RESPONSE_BODY_BYTES,
    AgentResponse,
    PublishResponse,
    report_error_details,
)


def _positive_id(value: str) -> int:
    try:
        number = int(value)
        if 1 <= number <= 2**63 - 1:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("response ID must be a positive integer")


def add_response_parsers(subparsers) -> None:
    publish = subparsers.add_parser(
        "respond", help="save an answer and send it to your Telegram topic"
    )
    source = publish.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="question/answer/action JSON file, or - for stdin")
    source.add_argument("--example", action="store_true", help="print an example to adapt")
    source.add_argument("--schema", action="store_true", help="print the response schema")
    publish.add_argument("--agent", help="author (default: $BACKBONE_AGENT)")
    publish.add_argument("--key", help="retry key (default: hash of content and prior response ID)")
    publish.add_argument(
        "--supersedes", type=_positive_id, help="explicitly update your prior response"
    )
    publish.add_argument(
        "--validate", action="store_true", help="validate --file without publishing"
    )
    publish.add_argument("--json", action="store_true")
    publish.set_defaults(func=cmd_respond)

    read = subparsers.add_parser(
        "responses", help="read saved answers separately from progress reports"
    )
    read.add_argument("--agent", help="filter by current author name")
    read.add_argument("--before", type=_positive_id, help="read older response IDs")
    read.add_argument("--limit", type=int, choices=range(1, 101), metavar="1–100", default=20)
    read.add_argument("--json", action="store_true")
    show = read.add_subparsers(dest="response_command").add_parser(
        "show", help="read one complete answer"
    )
    show.add_argument("id", type=_positive_id)
    show.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    read.set_defaults(func=lambda args: asyncio.run(_read(args)))


def _error(args, detail, code=1) -> int:
    if args.json:
        _common.print_json({"error": "response_error", "detail": detail})
    else:
        print(json.dumps(detail) if isinstance(detail, list) else str(detail), file=sys.stderr)
    return code


def cmd_respond(args) -> int:
    if args.validate and not args.file:
        return _error(args, "--validate requires --file", 2)
    if args.example:
        _common.print_json(
            {
                "question": "Is the change ready to use?",
                "answer": "The fix is merged and its checks passed.",
                "action": "Try the updated command. No decision is needed.",
            }
        )
        return 0
    if args.schema:
        _common.print_json(AgentResponse.model_json_schema())
        return 0
    try:
        if args.file == "-":
            raw = getattr(sys.stdin, "buffer", sys.stdin).read(RESPONSE_BODY_BYTES + 1)
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
        else:
            with Path(args.file).open("rb") as source:
                raw = source.read(RESPONSE_BODY_BYTES + 1)
        if len(raw) > RESPONSE_BODY_BYTES:
            return _error(args, f"response exceeds {RESPONSE_BODY_BYTES} UTF-8 bytes", 2)
        response = AgentResponse.model_validate_json(raw)
        if args.validate:
            _common.print_json({"valid": True}) if args.json else print("Valid response.")
            return 0
        agent = args.agent or os.environ.get("BACKBONE_AGENT", "").strip()
        if not agent:
            return _error(args, "Pass --agent NAME or run inside your agent session.", 2)
        digest = hashlib.sha256(
            json.dumps([response.model_dump(), args.supersedes], sort_keys=True).encode()
        ).hexdigest()
        publication = PublishResponse(
            agent=agent,
            request_id=args.key or digest,
            response=response,
            supersedes_id=args.supersedes,
        )
    except ValidationError as exc:
        return _error(args, report_error_details(exc.errors()), 2)
    except (OSError, ValueError) as exc:
        return _error(args, str(exc), 2)
    return asyncio.run(_publish(args, publication))


async def _publish(args, publication) -> int:
    result = await _common.api(
        await _common.read_client_config(),
        "POST",
        "/api/responses",
        json_body=publication.model_dump(),
    )
    if result is None:
        return _error(args, "Backbone is unreachable. Retry the same response/key when it is back.")
    status, data = result
    if status not in {200, 201}:
        return _error(args, data.get("detail", f"HTTP {status}"))
    if args.json:
        _common.print_json(data)
    else:
        verb = "Published" if data["created"] else "Already published"
        print(f"{verb} response {data['record']['id']}.")
        print(f"Read it: backbone responses show {data['record']['id']}")
        print(f"Telegram text: {data['record']['telegram_delivery']}")
    return 0


def _print_response(record) -> None:
    fields = [
        ("Author", record["agent_name"] or record["author_name"]),
        ("Published", record["created_at"]),
        ("Question", record["response"]["question"]),
        ("Answer", record["response"]["answer"]),
        ("Action or decision", record["response"]["action"]),
    ]
    if record.get("supersedes_id"):
        fields.append(("Updates response", str(record["supersedes_id"])))
    if record.get("superseded_by"):
        fields.append(("Updated by response", str(record["superseded_by"])))
    fields.append(("Telegram text", record["telegram_delivery"]))
    fields.append(("Telegram audio", record["telegram_audio_delivery"]))
    print_record(f"Response {record['id']}", fields)


async def _read(args) -> int:
    params = {"limit": args.limit}
    if args.agent:
        params["agent"] = args.agent
    if args.before:
        params["before_id"] = args.before
    show = args.response_command == "show"
    path = f"/api/responses/{args.id}" if show else "/api/responses?" + urlencode(params)
    result = await _common.api(await _common.read_client_config(), "GET", path)
    if result is None:
        return _error(args, "Backbone is unreachable; no responses were read.")
    status, data = result
    if status != 200:
        return _error(args, data.get("detail", f"HTTP {status}"))
    if args.json:
        _common.print_json(data)
    elif show:
        _print_response(data)
    else:
        for record in data["items"]:
            _print_response(record)
        if not data["items"]:
            print("No retained responses.")
        if data.get("has_more"):
            print(f"Older responses: repeat with --before {data['next_before_id']}")
    return 0
