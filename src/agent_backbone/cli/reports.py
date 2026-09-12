"""Structured authoring tools and concise progress/history views."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shlex
import sys
from pathlib import Path
from urllib.parse import urlencode

from pydantic import ValidationError

from agent_backbone.cli import _common
from agent_backbone.cli.presentation import note, print_record
from agent_backbone.models import (
    REPORT_BODY_BYTES,
    REPORT_LINKS,
    REPORT_TEXT_CHARACTERS,
    REPORTS_PER_HOUR,
    ProgressReport,
    PublishReport,
    ReportQuery,
    report_error_details,
    report_example,
)


def _limit(value: str) -> int:
    try:
        number = int(value)
        if 1 <= number <= 20:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("limit must be an integer from 1 to 20")


def _report_id(value: str) -> int:
    try:
        number = int(value)
        if 1 <= number <= 2**63 - 1:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("report ID must be a positive integer")


def add_report_parsers(subparsers) -> None:
    report = subparsers.add_parser("report", help="publish a brief structured progress report")
    source = report.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="report JSON file, or - for stdin")
    source.add_argument("--example", action="store_true", help="print a synthetic report to adapt")
    source.add_argument(
        "--schema", action="store_true", help="print the authoring schema and limits"
    )
    report.add_argument("--agent", help="author (default: $BACKBONE_AGENT)")
    report.add_argument("--key", help="publication retry key (default: hash of the report)")
    report.add_argument(
        "--validate", action="store_true", help="validate --file without publishing"
    )
    report.add_argument("--json", action="store_true", help="machine-readable receipt or errors")
    report.set_defaults(func=cmd_report)

    updates = subparsers.add_parser("updates", help="read saved progress reports from the agents")
    updates.add_argument(
        "--agent", action="append", default=[], help="select an agent (repeatable)"
    )
    updates.add_argument(
        "--history", action="store_true", help="read previous reports, newest first"
    )
    updates.add_argument("--author-id", help="history for an author, including a forgotten agent")
    updates.add_argument(
        "--members", action="store_true", help="include swarm members in the shared feed"
    )
    updates.add_argument("--limit", type=_limit, default=5, help="page size, 1–20 (default: 5)")
    updates.add_argument("--cursor", help="read the next page using the returned cursor")
    updates.add_argument("--json", action="store_true", help="the same feed for an orchestrator")
    commands = updates.add_subparsers(dest="updates_command")
    show = commands.add_parser("show", help="read a full report and its titled links")
    show.add_argument("id", type=_report_id)
    show.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    updates.set_defaults(func=cmd_updates)


def _error(args, code: str, detail, exit_code: int = 1) -> int:
    if args.json:
        _common.print_json({"error": code, "detail": detail})
    elif isinstance(detail, list):
        for item in detail[:10]:
            print(
                f"{item.get('field', 'report')}: {item.get('message', 'invalid value')}",
                file=sys.stderr,
            )
    else:
        print(str(detail)[:1000], file=sys.stderr)
    return exit_code


def _validation(exc: ValidationError) -> list[dict]:
    return report_error_details(exc.errors())


def _load_report(path: str) -> ProgressReport:
    if path == "-":
        reader = getattr(sys.stdin, "buffer", sys.stdin)
        raw = reader.read(REPORT_BODY_BYTES + 1)
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
    else:
        with Path(path).open("rb") as source:
            raw = source.read(REPORT_BODY_BYTES + 1)
    if len(raw) > REPORT_BODY_BYTES:
        raise ValueError(f"report file exceeds {REPORT_BODY_BYTES} UTF-8 bytes; shorten it")
    return ProgressReport.model_validate_json(raw)


async def _publish(args, publication: PublishReport) -> int:
    config = await _common.read_client_config()
    result = await _common.api(config, "POST", "/api/reports", json_body=publication.model_dump())
    if result is None:
        return _error(
            args,
            "reports_unavailable",
            "Backbone is unreachable. Retry the same report/key when it is back.",
        )
    status, data = result
    if status not in {200, 201}:
        return _error(args, "report_rejected", data.get("detail", f"API returned HTTP {status}"))
    if args.json:
        _common.print_json(data)
    else:
        verb = "Published" if data["created"] else "Already published"
        print(f"{verb} report {data['record']['id']} for {data['record']['agent_name']}.")
        print(f"Read it: backbone updates show {data['record']['id']}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    if args.validate and not args.file:
        return _error(args, "invalid_report", "--validate requires --file", 2)
    if args.example:
        _common.print_json(report_example())
        return 0
    if args.schema:
        _common.print_json(
            ProgressReport.model_json_schema()
            | {
                "x-report-limits": {
                    "request_bytes": REPORT_BODY_BYTES,
                    "text_and_title_characters": REPORT_TEXT_CHARACTERS,
                    "links": REPORT_LINKS,
                    "reports_per_agent_per_hour": REPORTS_PER_HOUR,
                }
            }
        )
        return 0
    try:
        report = _load_report(args.file)
        if args.validate:
            if args.json:
                _common.print_json({"valid": True})
            else:
                print("Valid report. Ready to publish.")
            return 0
        name = args.agent or os.environ.get("BACKBONE_AGENT", "").strip()
        if not name:
            return _error(
                args,
                "invalid_report",
                "Pass --agent NAME; inside an agent session $BACKBONE_AGENT supplies it.",
                2,
            )
        digest = hashlib.sha256(
            json.dumps(report.model_dump(), sort_keys=True).encode()
        ).hexdigest()
        publication = PublishReport(agent=name, request_id=args.key or digest, report=report)
    except ValidationError as exc:
        return _error(args, "invalid_report", _validation(exc), 2)
    except (OSError, ValueError) as exc:
        return _error(args, "invalid_report", str(exc), 2)
    return asyncio.run(_publish(args, publication))


def _age(record: dict) -> str:
    seconds = record["age_seconds"]
    value = (
        "just now"
        if seconds < 60
        else f"{seconds // 60}m ago"
        if seconds < 3600
        else f"{seconds // 3600}h ago"
        if seconds < 86400
        else f"{seconds // 86400}d ago"
    )
    return value + ("; old report" if record["stale"] else "")


def _print_record(record: dict) -> None:
    name = record["agent_name"] or f"{record['author_name']} (forgotten)"
    report = ProgressReport.model_validate(record["report"])
    note(f"{name} — {report.status} — report {record['id']} — {_age(record)}")
    print(f"Published {record['created_at']} as {record['author_name']}")
    fields = []
    for key, section in report.sections():
        label = key.capitalize()
        if key == "blockers":
            label += f" ({report.blockers.kind})"
        value = "\n".join([section.text, *(f"{link.title}: {link.url}" for link in section.links)])
        fields.append((label, value))
    print_record("Report", fields)
    print(f"Author history: backbone updates --history --author-id {record['author_id']}")


def _print_page(data: dict, query: ReportQuery) -> None:
    print("Report history" if data["history"] else "Agent updates — latest reports")
    for entry in data["items"]:
        record = entry["record"]
        if record is None:
            note(f"\n{entry['agent_name']} — no report yet")
            continue
        report = record["report"]
        name = record["agent_name"] or f"{record['author_name']} (forgotten)"
        note(f"\n{name} — {report['status']} — {_age(record)} — report {record['id']}")
        fields = []
        for key in ("goal", "progress", "blockers", "next"):
            label = key.capitalize()
            if key == "blockers":
                label += f" ({report[key]['kind']})"
            fields.append((label, report[key]["text"]))
        print_record("Report", fields)
    if not data["items"]:
        print("No retained reports." if data["history"] else "No agents in this view.")
    print("\nFull report and links: backbone updates show ID")
    if data.get("next_cursor"):
        command = ["backbone", "updates"]
        for agent in query.agents:
            command += ["--agent", agent]
        if query.history:
            command += ["--history"]
        if query.author_id:
            command += ["--author-id", query.author_id]
        if query.members:
            command += ["--members"]
        command += ["--limit", str(query.limit), "--cursor", data["next_cursor"]]
        print("Older / more: " + shlex.join(command))


async def _updates(args: argparse.Namespace) -> int:
    try:
        query = ReportQuery(
            agents=args.agent,
            author_id=args.author_id,
            history=args.history,
            members=args.members,
            limit=args.limit,
            cursor=args.cursor,
        )
    except ValidationError as exc:
        return _error(args, "invalid_query", _validation(exc), 2)
    show = args.updates_command == "show"
    params = {
        "agent": query.agents,
        "history": str(query.history).lower(),
        "members": str(query.members).lower(),
        "limit": query.limit,
    }
    if query.cursor:
        params["cursor"] = query.cursor
    if query.author_id:
        params["author_id"] = query.author_id
    path = f"/api/reports/{args.id}" if show else "/api/reports?" + urlencode(params, doseq=True)
    result = await _common.api(await _common.read_client_config(), "GET", path)
    if result is None:
        return _error(args, "reports_unavailable", "Backbone is unreachable; no reports were read.")
    status, data = result
    if status != 200:
        return _error(
            args, "reports_unavailable", data.get("detail", f"API returned HTTP {status}")
        )
    if args.json:
        _common.print_json(data)
    elif show:
        _print_record(data)
    else:
        _print_page(data, query)
    return 0


def cmd_updates(args: argparse.Namespace) -> int:
    return asyncio.run(_updates(args))
