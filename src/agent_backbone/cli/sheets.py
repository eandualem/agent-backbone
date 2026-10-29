"""Runtime-independent authoring of retained spreadsheet workbooks."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlencode

import httpx
from pydantic import ValidationError

from agent_backbone.cli import _common
from agent_backbone.cli.presentation import print_record, print_table
from agent_backbone.sheets import (
    SHEET_WIRE_BYTES,
    CreateSheet,
    EditSheet,
    SheetFeedback,
    SheetImport,
)

_MODELS = {
    "create": CreateSheet,
    "edit": EditSheet,
    "feedback": SheetFeedback,
    "import": SheetImport,
}


def add_sheet_parsers(subparsers):
    parser = subparsers.add_parser("sheets", help="create, edit and read retained spreadsheets")
    parser.add_argument("--author")
    parser.add_argument("--author-id")
    parser.add_argument("--cursor")
    parser.add_argument("--limit", type=int, choices=range(1, 101), default=50, metavar="1–100")
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="sheet_command")
    for name in ("create", "edit", "feedback", "import"):
        command = sub.add_parser(name, help=f"{name} a sheet from a file or stdin")
        if name in {"edit", "feedback"}:
            command.add_argument("sheet_id")
        source = command.add_mutually_exclusive_group(required=True)
        source.add_argument(
            "--file", help="payload JSON, or - for stdin; import accepts the named format"
        )
        source.add_argument("--schema", action="store_true")
        command.add_argument(
            "--validate",
            action="store_true",
            help="validate request shape without saving or calculating",
        )
        command.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        if name == "import":
            command.add_argument("--format", choices=("json", "csv", "tsv", "xlsx"))
            command.add_argument("--title")
            command.add_argument("--agent", default=os.environ.get("BACKBONE_AGENT"))
            command.add_argument(
                "--key", help="UUID-hex retry key; defaults to a stable content fingerprint"
            )
    show = sub.add_parser("show", help="read metadata or a revision-pinned range")
    show.add_argument("sheet_id")
    show.add_argument("--revision")
    show.add_argument("--tab")
    show.add_argument("--row-offset", type=int, default=0)
    show.add_argument("--row-limit", type=int, default=100)
    show.add_argument("--column-offset", type=int, default=0)
    show.add_argument("--column-limit", type=int, default=20)
    show.add_argument("--order", choices=("canonical", "view"), default="canonical")
    history = sub.add_parser("history", help="read immutable revision metadata")
    history.add_argument("sheet_id")
    history.add_argument("--cursor", default=argparse.SUPPRESS)
    history.add_argument(
        "--limit", type=int, choices=range(1, 101), default=argparse.SUPPRESS, metavar="1–100"
    )
    export = sub.add_parser("export", help="export one pinned revision to a portable file")
    export.add_argument("sheet_id")
    export.add_argument("--revision", required=True)
    export.add_argument("--format", choices=("json", "csv", "tsv", "xlsx"), required=True)
    export.add_argument("--tab")
    export.add_argument("--output", required=True, help="new output file, or - for stdout")
    for command in (show, history, export):
        command.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    parser.set_defaults(func=cmd_sheets)


def _error(args, message, code=1):
    if args.json:
        _common.print_json({"error": "sheet_error", "detail": message})
    else:
        print_record("Sheet error", [("Detail", json.dumps(message, ensure_ascii=True))])
    return code


def _load(args):
    if args.file == "-":
        raw = getattr(sys.stdin, "buffer", sys.stdin).read(SHEET_WIRE_BYTES + 1)
        if isinstance(raw, str):
            raw = raw.encode()
    else:
        with Path(args.file).open("rb") as source:
            raw = source.read(SHEET_WIRE_BYTES + 1)
    if len(raw) > SHEET_WIRE_BYTES:
        raise ValueError("file exceeds sheet wire limit")
    if args.sheet_command == "import":
        if not args.format or not args.title or not args.agent:
            raise ValueError("import requires --format, --title and --agent (or BACKBONE_AGENT)")
        content = base64.b64encode(raw).decode() if args.format == "xlsx" else raw.decode("utf-8")
        body = {
            "author": args.agent,
            "title": args.title,
            "format": args.format,
            "content": content,
        }
        body["request_id"] = (
            args.key or hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:32]
        )
    else:
        body = json.loads(raw)
    model = _MODELS[args.sheet_command].model_validate(body)
    payload = model.model_dump(exclude_unset=True)
    if len(json.dumps(payload).encode()) > (
        SHEET_WIRE_BYTES if args.sheet_command in {"create", "import"} else 1024 * 1024
    ):
        raise ValueError("encoded request exceeds wire limit")
    return payload


def cmd_sheets(args):
    payload = None
    if args.sheet_command in _MODELS:
        if args.schema:
            _common.print_json(_MODELS[args.sheet_command].model_json_schema())
            return 0
        try:
            payload = _load(args)
        except ValidationError as exc:
            return _error(
                args, [{"type": e["type"], "msg": e["msg"][:240]} for e in exc.errors()[:10]], 2
            )
        except (OSError, ValueError) as exc:
            return _error(args, str(exc), 2)
        if args.validate:
            _common.print_json(
                {"valid": True, "scope": "request shape; no calculation or publication"}
            )
            return 0
    return asyncio.run(_run(args, payload))


async def _run(args, payload):
    command = args.sheet_command
    config = await _common.read_client_config()
    path, params, method = "/api/sheets", {}, "GET"
    if command in {"show", "history", "edit", "feedback", "export"}:
        # Avoid treating user IDs as path segments or query injection.
        import re

        if not re.fullmatch(r"[0-9a-f]{32}", args.sheet_id):
            return _error(args, "sheet_id must be 32 lowercase hexadecimal characters", 2)
        path += "/" + args.sheet_id
    if payload is not None:
        method = "POST"
        path += {"create": "", "import": "/import", "edit": "/revisions", "feedback": "/feedback"}[
            command
        ]
    elif command in {None, "history"}:
        if command == "history":
            path += "/revisions"
        params = {"limit": args.limit, "cursor": args.cursor}
        if command is None:
            params.update(author=args.author, author_id=args.author_id)
    elif command == "show":
        params = {"revision_id": args.revision}
        if args.tab:
            if not args.revision or not re.fullmatch(r"[0-9a-f]{32}", args.tab):
                return _error(args, "range reads require --revision and a UUID-hex --tab", 2)
            path += f"/tabs/{args.tab}/range"
            params.update(
                row_offset=args.row_offset,
                row_limit=args.row_limit,
                column_offset=args.column_offset,
                column_limit=args.column_limit,
                order=args.order,
            )
    elif command == "export":
        path += "/export"
        params = {"revision_id": args.revision, "format": args.format, "tab_id": args.tab}
    params = {k: v for k, v in params.items() if v is not None}
    if params:
        path += "?" + urlencode(params)
    if command == "export":
        return await _export(args, config, path)
    result = await _common.api(config, method, path, json_body=payload, timeout=20)
    if result is None:
        return _error(
            args, "No response. Retry the exact request and request_id; do not create a new key."
        )
    status, data = result
    if status not in {200, 201}:
        return _error(args, data.get("detail", f"HTTP {status}"))
    if args.json:
        _common.print_json(data)
    elif "items" in data:
        print_table(
            "Sheets" if command is None else "Sheet revisions",
            ("Sheet", "Title", "Revision", "Updated"),
            ((r["sheet_id"], r["title"], r["revision_id"], r["updated_at"]) for r in data["items"]),
            empty="No retained sheets.",
        )
        if data.get("next_cursor"):
            print("Continue with --cursor " + data["next_cursor"])
    elif "rows" in data:
        values = {(c["row_id"], c["column_id"]): c["display"] for c in data["cells"]}
        print_table(
            "Sheet range · " + data["revision_id"],
            ("Row", *(str(c["index"]) for c in data["columns"])),
            (
                (
                    str(r["index"]),
                    *(values.get((r["row_id"], c["column_id"]), "") for c in data["columns"]),
                )
                for r in data["rows"]
            ),
        )
        print("Use --json for stable row/column IDs and typed values.")
    elif "feedback_id" in data:
        print_record(
            "Sheet feedback",
            [("Feedback", data["feedback_id"]), ("Delivery", data["delivery"]["detail"])],
        )
    else:
        record = data.get("record", data)
        print_record(
            "Sheet",
            [
                ("Title", record["title"]),
                ("Sheet", record["sheet_id"]),
                ("Revision", record["revision_id"]),
                ("Author", record["agent_name"] or record["author_name"]),
                ("Tabs", ", ".join(t["name"] for t in record["tabs"])),
            ],
        )
        for warning in data.get("warnings", []):
            print("Import note: " + warning)
    return 0


async def _export(args, config, path):
    if args.output == "-" and args.json:
        return _error(args, "--json requires a file destination, not --output -", 2)
    try:
        async with (
            httpx.AsyncClient(timeout=20) as client,
            client.stream(
                "GET", _common.api_url(config, path), headers=_common.headers(config)
            ) as response,
        ):
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                if len(raw) + len(chunk) > SHEET_WIRE_BYTES:
                    return _error(args, "export response exceeds byte limit")
                raw.extend(chunk)
            if response.status_code != 200:
                return _error(args, json.loads(raw).get("detail", f"HTTP {response.status_code}"))
            warnings = json.loads(response.headers.get("X-Backbone-Sheet-Warnings", "[]"))
        if args.output == "-":
            sys.stdout.buffer.write(raw)
            for warning in warnings:
                print("Export note: " + warning, file=sys.stderr)
        else:
            with Path(args.output).open("xb") as destination:
                destination.write(raw)
            if args.json:
                _common.print_json({"output": args.output, "bytes": len(raw), "warnings": warnings})
            else:
                print_record(
                    "Sheet export",
                    [("File", args.output), ("Bytes", len(raw)), ("Notes", " ".join(warnings))],
                )
        return 0
    except (httpx.HTTPError, OSError, ValueError) as exc:
        return _error(args, str(exc))
