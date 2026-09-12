"""A bounded, metadata-only view of operational diagnostics."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from agent_backbone.cli import _common
from agent_backbone.cli.presentation import note, print_record, print_table


def parse_since(value: str) -> str:
    """Accept a positive duration or an ISO timestamp with an explicit timezone."""
    match = re.fullmatch(r"([1-9][0-9]*)([mhd])", value)
    try:
        if match:
            seconds = int(match[1]) * {"m": 60, "h": 3600, "d": 86400}[match[2]]
            parsed = datetime.now(UTC) - timedelta(seconds=seconds)
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("timestamp requires a timezone")
        return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    except (ValueError, OverflowError) as exc:
        raise argparse.ArgumentTypeError(
            "use a positive duration such as 30m, 24h or 7d, or an ISO timestamp with a timezone"
        ) from exc


def _limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("limit must be an integer from 1 to 100") from exc
    if not 1 <= parsed <= 100:
        raise argparse.ArgumentTypeError("limit must be an integer from 1 to 100")
    return parsed


def _record_id(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("record ID must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("record ID must be a positive integer")
    return parsed


def _operation_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_./:@+\-]{1,200}", value):
        raise argparse.ArgumentTypeError(
            "operation ID must be a nonempty identifier without spaces"
        )
    return value


def add_diagnostics_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "diagnostics", help="summarize recorded operational problems without message content"
    )
    parser.add_argument(
        "--since", type=parse_since, default="24h", help="duration or ISO time (default: 24h)"
    )
    parser.add_argument("--agent", default=None, help="only this agent's diagnostics")
    parser.add_argument("--limit", type=_limit, default=20, help="group limit, 1–100 (default: 20)")
    parser.add_argument("--json", action="store_true", help="print structured metadata")
    commands = parser.add_subparsers(dest="diagnostics_command")
    show = commands.add_parser("show", help="inspect a record and its operation's diagnostics")
    show.add_argument("id", type=_record_id)
    show.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    trace = commands.add_parser("trace", help="inspect the exact operation ID from a receipt")
    trace.add_argument("operation_id", type=_operation_id)
    trace.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    parser.set_defaults(func=cmd_diagnostics)


def _error(args: argparse.Namespace, detail: str) -> int:
    if args.json:
        _common.print_json({"error": "diagnostics_unavailable", "detail": detail})
    else:
        note(f"Diagnostics unavailable: {detail}")
    return 1


def _print_digest(data: dict) -> None:
    note(f"Diagnostics since {data.get('since') or 'the oldest retained record'}")
    groups = data["groups"]
    note(
        f"  {data.get('total_groups', len(groups))} problem group(s), "
        f"{data.get('total_occurrences', 0)} retained occurrence(s)"
    )
    print_table(
        "Problem groups",
        ("Severity", "Code", "Agent / Issue", "Occurrences", "Latest / Inspect"),
        [
            (
                group["severity"],
                group["code"],
                (group.get("agent_name") or "backbone")
                + (
                    f"\n{group.get('repo', '')}#{group['issue_number']}"
                    if group.get("issue_number") is not None
                    else ""
                ),
                group["occurrences"],
                f"{group['last_seen_at']}\nshow {group['sample_id']}",
            )
            for group in groups
        ],
        empty="No recorded problem groups in this interval.",
    )
    if data.get("has_more"):
        note("  More groups exist; narrow --since or --agent, or increase --limit.")
    informational = data.get("informational", {})
    if informational:
        print_table(
            "Other recorded outcomes", ("Code", "Occurrences"), sorted(informational.items())
        )
    deliveries = data.get("deliveries", {})
    if deliveries:
        note(f"  Delivery history: {deliveries.get('attempts', 0)} attempt(s)")
        print_table(
            "Delivery outcomes",
            ("Outcome", "Attempts"),
            sorted(deliveries.get("outcomes", {}).items()),
        )
    queue = data.get("queue", {})
    if queue:
        note(
            f"  Queue now: {queue.get('pending', 0)} pending, {queue.get('in_progress', 0)} leased"
        )
        if queue.get("oldest_pending_at"):
            note(f"    oldest pending: {queue['oldest_pending_at']}")
    note("  Groups were last seen in this interval; their counts include retained earlier repeats.")
    failures = data.get("coverage", {}).get("write_failures_since_process_start", 0)
    if failures:
        note(f"  Evidence incomplete: {failures} diagnostic write(s) failed in this process.")
    note("  An empty result does not establish system health.")
    note("  Investigation guide: backbone docs diagnostics")


def _print_record(data: dict) -> None:
    record = data["record"]
    note(f"Diagnostic {record['id']}: {record['code']} ({record['severity']})")
    fields = []
    for key in (
        "agent_name",
        "operation_id",
        "category",
        "source",
        "runtime",
        "model",
        "repo",
        "issue_number",
        "delivery_id",
        "queue_id",
        "event_id",
        "first_seen_at",
        "last_seen_at",
        "occurrences",
    ):
        if record.get(key) is not None and record[key] != "":
            fields.append((key, record[key]))
    for key, value in sorted(record.get("details", {}).items()):
        fields.append((key, json.dumps(value, ensure_ascii=True)))
    print_record("Evidence", fields)
    records = data.get("operation_records", [])
    if records:
        print_table(
            "Operation records",
            ("ID", "Code", "Severity", "Occurrences", "Last seen"),
            [
                (
                    item["id"],
                    item["code"],
                    item["severity"],
                    item["occurrences"],
                    item["last_seen_at"],
                )
                for item in records
            ],
        )
    if data.get("has_more"):
        note(
            "  More operation records exist; use GET /api/diagnostics/records to page through them."
        )


def _print_trace(data: dict) -> None:
    note(f"Operation {data['operation_id']}")
    note(f"  {data['count']} record(s); truncated: {'yes' if data['truncated'] else 'no'}")
    for item in data["items"]:
        _print_record({"record": item})
    if data["truncated"]:
        note(
            "  Read the next page using GET /api/diagnostics/records with this operation_id "
            f"and before_id={data['next_before_id']}."
        )


async def _diagnostics(args: argparse.Namespace) -> int:
    config = await _common.read_client_config()
    show = args.diagnostics_command == "show"
    trace = args.diagnostics_command == "trace"
    if show:
        path = f"/api/diagnostics/{args.id}"
    elif trace:
        path = "/api/diagnostics/records?" + urlencode(
            {"operation_id": args.operation_id, "limit": 100}
        )
    else:
        params = {"since": args.since, "limit": args.limit}
        if args.agent is not None:
            params["agent"] = args.agent
        path = f"/api/diagnostics?{urlencode(params)}"
    result = await _common.api(config, "GET", path)
    if result is None:
        return _error(args, "the backbone API is unreachable; no diagnostic result was read.")
    status, data = result
    if status != 200:
        if status == 404 and show:
            return _error(args, f"record {args.id} was not found; it may have been pruned.")
        return _error(args, f"the API returned HTTP {status}; no diagnostic result was read.")
    required = "record" if show else "items" if trace else "groups"
    expected = dict if show else list
    if not isinstance(data, dict) or not isinstance(data.get(required), expected):
        return _error(args, "the API returned an unexpected response.")
    if trace:
        if not data["items"]:
            return _error(
                args,
                "no retained diagnostics were found for that operation; "
                "it may be unrecorded or pruned.",
            )
        data = {
            "operation_id": args.operation_id,
            "count": len(data["items"]),
            "truncated": bool(data.get("has_more")),
            "items": data["items"],
            "next_before_id": data.get("next_before_id"),
        }
    if args.json:
        _common.print_json(data)
    elif trace:
        _print_trace(data)
    elif show:
        _print_record(data)
    else:
        _print_digest(data)
    return 0


def cmd_diagnostics(args: argparse.Namespace) -> int:
    return asyncio.run(_diagnostics(args))
