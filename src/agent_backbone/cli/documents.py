"""Publish exact UTF-8 HTML and retrieve retained immutable revisions."""

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
from agent_backbone.cli.presentation import print_record, print_table
from agent_backbone.models import DOCUMENT_HTML_BYTES, PublishDocument, report_error_details


def _positive_id(value: str) -> int:
    try:
        number = int(value)
        if 1 <= number <= 2**63 - 1:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("revision ID must be a positive integer")


def add_document_parsers(subparsers) -> None:
    read = subparsers.add_parser("documents", help="publish and read durable HTML Documents")
    read.add_argument("--agent", help="filter by current author name")
    read.add_argument(
        "--author-id", help="filter by stable author identity, including forgotten agents"
    )
    read.add_argument("--before", type=_positive_id, help="read latest revisions below this ID")
    read.add_argument("--limit", type=int, choices=range(1, 101), metavar="1–100", default=20)
    read.add_argument("--json", action="store_true")
    sub = read.add_subparsers(dest="document_command")
    publish = sub.add_parser("publish", help="retain an HTML file; no notification is sent")
    source = publish.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="UTF-8 HTML file, or - for stdin")
    source.add_argument("--schema", action="store_true", help="print the publication JSON schema")
    publish.add_argument("--title", help="document title, preserved exactly")
    publish.add_argument(
        "--agent", default=argparse.SUPPRESS, help="author (default: $BACKBONE_AGENT)"
    )
    publish.add_argument("--key", help="retry key (default: hash of title, HTML and predecessor)")
    publish.add_argument("--supersedes", type=_positive_id, help="correct your current revision")
    publish.add_argument(
        "--validate", action="store_true", help="check the file without publishing"
    )
    publish.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    show = sub.add_parser("show", help="read revision metadata or export its exact HTML")
    show.add_argument("id", type=_positive_id)
    show.add_argument("--output", help="save exact HTML bytes to this file")
    show.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    read.set_defaults(func=cmd_documents)


def _error(args, detail, code=1):
    if args.json:
        _common.print_json({"error": "document_error", "detail": detail})
    else:
        print_record("Document error", [("Detail", json.dumps(detail, ensure_ascii=True))])
    return code


def cmd_documents(args) -> int:
    if args.document_command != "publish":
        return asyncio.run(_read(args))
    if args.schema:
        _common.print_json(PublishDocument.model_json_schema())
        return 0
    if not args.title:
        return _error(args, "--title is required with --file", 2)
    try:
        if args.file == "-":
            raw = getattr(sys.stdin, "buffer", sys.stdin).read(DOCUMENT_HTML_BYTES + 1)
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
        else:
            with Path(args.file).open("rb") as source:
                raw = source.read(DOCUMENT_HTML_BYTES + 1)
        if len(raw) > DOCUMENT_HTML_BYTES:
            return _error(args, f"HTML exceeds {DOCUMENT_HTML_BYTES} UTF-8 bytes", 2)
        html = raw.decode("utf-8")
        author = args.agent or os.environ.get("BACKBONE_AGENT", "").strip()
        if not author and not args.validate:
            return _error(args, "Pass --agent NAME or run inside your agent session.", 2)
        key = (
            args.key
            or hashlib.sha256(
                json.dumps([args.title, html, args.supersedes], ensure_ascii=True).encode()
            ).hexdigest()
        )
        publication = PublishDocument(
            author=author or "validation",
            request_id=key,
            title=args.title,
            html=html,
            supersedes_id=args.supersedes,
        )
        if args.validate:
            _common.print_json({"valid": True}) if args.json else print("Valid document.")
            return 0
    except ValidationError as exc:
        return _error(args, report_error_details(exc.errors()), 2)
    except (OSError, ValueError) as exc:
        return _error(args, str(exc), 2)
    return asyncio.run(_publish(args, publication))


async def _publish(args, publication) -> int:
    result = await _common.api(
        await _common.read_client_config(),
        "POST",
        "/api/documents",
        json_body=publication.model_dump(),
    )
    if result is None:
        return _error(
            args, "Backbone is unreachable. Retry the same content and key when it returns."
        )
    status, data = result
    if status not in {200, 201}:
        return _error(args, data.get("detail", f"HTTP {status}"))
    if args.json:
        _common.print_json(data)
    else:
        verb = "Published" if data["created"] else "Already published"
        print(f"{verb} document revision {data['record']['id']}.")
        print(f"Read it: backbone documents show {data['record']['id']}")
    return 0


async def _read(args) -> int:
    show = args.document_command == "show"
    params = {"limit": args.limit}
    for key, value in (
        ("agent", args.agent),
        ("author_id", args.author_id),
        ("before_id", args.before),
    ):
        if value is not None:
            params[key] = value
    path = f"/api/documents/{args.id}" if show else "/api/documents?" + urlencode(params)
    result = await _common.api(await _common.read_client_config(), "GET", path)
    if result is None:
        return _error(args, "Backbone is unreachable; no documents were read.")
    status, data = result
    if status != 200:
        return _error(args, data.get("detail", f"HTTP {status}"))
    if show and args.output:
        try:
            Path(args.output).write_bytes(data["html"].encode("utf-8"))
        except OSError as exc:
            return _error(args, str(exc))
    if args.json:
        _common.print_json(data)
    elif show:
        print_record(
            f"Document revision {data['id']}",
            [
                ("Title", data["title"]),
                ("Document", data["document_id"]),
                ("Author", data["agent_name"] or data["author_name"]),
                ("Author ID", data["author_id"]),
                ("Published", data["created_at"]),
                ("HTML bytes", data["content_bytes"]),
                ("SHA-256", data["content_sha256"]),
                ("Previous revision", data["supersedes_id"]),
                ("Next revision", data["superseded_by"]),
                ("HTML", args.output or "Use --json for full content or --output FILE to save it."),
            ],
        )
    else:
        print_table(
            "Documents",
            ("Revision", "Title", "Author", "Published"),
            (
                (r["id"], r["title"], r["agent_name"] or r["author_name"], r["created_at"])
                for r in data["items"]
            ),
            empty="No retained documents.",
        )
        if data["has_more"]:
            print(f"More documents: repeat with --before {data['next_before_id']}")
    return 0
