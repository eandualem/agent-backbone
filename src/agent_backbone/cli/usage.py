"""Token-first views by agent and CLI conversation, with optional price detail."""

from __future__ import annotations

import argparse
import asyncio
import sys
from urllib.parse import urlencode

from agent_backbone.cli import _common
from agent_backbone.cli.diagnostics import parse_since
from agent_backbone.cli.presentation import note, print_record, print_table
from agent_backbone.services.agents import usage_view
from agent_backbone.usage import TOKEN_FIELDS, timestamp


def _tokens(row: dict, key: str) -> str:
    return f"{row[key]:,}" if row["observations"] else "unavailable"


def _cost(row: dict) -> str:
    if row["estimated_usd"] is None:
        return "unpriced"
    return f"${float(row['estimated_usd']):.4f}" + (
        " (partial)" if row["cost_coverage"] != "complete" else ""
    )


def print_usage(data: dict, args: argparse.Namespace) -> None:
    if args.view == "limits":
        print_table(
            "Reported subscription allowance snapshots",
            ("Agent / CLI", "Observed", "Bucket", "Window / Used / Reset"),
            [
                (
                    f"{r['agent_name']} / {r['runtime']}",
                    r["observed_at"],
                    r.get("bucket") or "unknown",
                    "\n".join(
                        f"{w.get('name')} ({w.get('window_minutes', '?')} min): "
                        f"{w.get('used_percent', '?')}% / "
                        f"{timestamp(w['resets_at']) if 'resets_at' in w else 'unknown reset'}"
                        + (" (expired)" if w.get("expired") else "")
                        for w in r["windows"]
                    ),
                )
                for r in data["limits"]
            ],
            empty="No supported allowance snapshot is available.",
        )
        note(
            "Account windows can be shared by several sessions; do not add their percentages. "
            "These are last observed snapshots, not a live billing query."
        )
        return
    if args.view == "session":
        for session in data["sessions"]:
            print_record(
                f"{session['agent_name']} / {session['runtime']} / {session['session_id']}",
                [
                    ("Current session", session["current"]),
                    ("Models", ", ".join(session["models"])),
                    ("Parent session", session["parent_id"]),
                    ("Coverage", session["coverage"]),
                    ("Collected", session["last_seen"]),
                    ("Last usage", session["last_usage_at"]),
                    ("Detail", session["detail"]),
                    ("Launches", len(session["launches"])),
                ],
            )
        print_table(
            "Request observations",
            ("Time", "Model", "Input", "Cache read / write", "Output", "Total"),
            [
                (
                    e["at"],
                    e["model"],
                    f"{e['input_tokens']:,}",
                    f"{e['cache_read_tokens']:,} / {e['cache_write_tokens']:,}",
                    f"{e['output_tokens']:,}",
                    f"{sum(e[k] for k in TOKEN_FIELDS):,}",
                )
                for e in data["items"]
            ],
        )
        if args.cost:
            for e in data["items"]:
                print_record(
                    e["key"],
                    [
                        ("Model", e["model"]),
                        ("API-equivalent USD", e["cost"]["usd"] or "unpriced"),
                        ("Price basis", e["cost"]["basis"]),
                        ("Pricing detail", e["cost"]["reason"]),
                    ],
                )
    elif args.by == "model":
        columns = ["Model", "Input", "Cache read / write", "Output", "Total"]
        rows = [
            (
                r["model"],
                _tokens(r, "input_tokens"),
                f"{r['cache_read_tokens']:,} / {r['cache_write_tokens']:,}",
                _tokens(r, "output_tokens"),
                _tokens(r, "total_tokens"),
            )
            for r in data["items"]
        ]
        if args.cost:
            columns.append("API estimate")
            rows = [(*row, _cost(r)) for row, r in zip(rows, data["items"], strict=True)]
        print_table("Usage by observed model", columns, rows)
    else:
        columns = ["Agent / CLI", "Session", "Models", "Tokens", "Coverage"]
        rows = [
            (
                f"{s['agent_name']} / {s['runtime']}",
                s["id"]
                + ("\ncurrent" if s["current"] else "")
                + ("\nchild" if s["parent_id"] else ""),
                "\n".join(s["models"]) or "unknown",
                _tokens(s, "total_tokens"),
                s["coverage"],
            )
            for s in data["items"]
        ]
        if args.cost:
            columns.append("API estimate")
            rows = [(*row, _cost(s)) for row, s in zip(rows, data["items"], strict=True)]
        print_table(
            "Token usage by CLI session", columns, rows, empty="No identified session usage yet."
        )
    total = data["totals"]
    print_record(
        "Selected usage",
        [
            ("Input (uncached)", _tokens(total, "input_tokens")),
            ("Cache read", _tokens(total, "cache_read_tokens")),
            ("Cache write", _tokens(total, "cache_write_tokens")),
            ("Output", _tokens(total, "output_tokens")),
            ("Total tokens", _tokens(total, "total_tokens")),
            ("Coverage", total["coverage"]),
            *(([("API estimate", _cost(total))]) if args.cost else []),
        ],
    )
    for missing in data["unavailable"]:
        note(f"{missing['agent_name']} / {missing['runtime']}: {missing['reason']}")
    for error in data["collection"]["errors"]:
        note("Collection: " + error)
    if not data["collection"]["enabled"]:
        note("Usage collection is disabled; displaying retained history.")
    if data["has_more"]:
        note(
            f"More entries: repeat with --offset {data['next_offset']}. "
            "Totals cover the whole selection."
        )
    note(
        "Session details: backbone usage session ID --cost\n"
        "Model totals: backbone usage --by model\nQuick-start guide: backbone help usage"
    )
    note(data["semantics"])


async def _usage(args: argparse.Namespace) -> int:
    if args.view == "session" and not args.session:
        raise ValueError("usage session requires a session ID")
    if args.view != "session" and args.session:
        raise ValueError("unexpected session ID")
    query = {
        "agent": args.agent,
        "runtime": args.runtime,
        "session": args.session,
        "since": parse_since(args.since) if args.since else None,
        "until": parse_since(args.until) if args.until else None,
        "by": args.by,
        "refresh": not args.no_refresh,
        "reprice": args.reprice,
        "current_only": args.current,
        "limit": args.limit,
        "offset": args.offset,
    }
    if not 1 <= args.limit <= 1000 or args.offset < 0:
        raise ValueError("limit must be 1–1000 and offset must be nonnegative")
    boot = await _common.read_client_config()
    if await _common.api_up(boot):
        encoded = urlencode(
            {
                k: str(v).lower() if isinstance(v, bool) else v
                for k, v in query.items()
                if v is not None
            }
        )
        response = await _common.api(boot, "GET", "/api/usage?" + encoded, timeout=60)
        if not response or response[0] != 200:
            raise ValueError(str(response[1] if response else "usage API unavailable"))
        data = response[1]
    else:
        async with _common.Direct(boot) as direct:
            data = await usage_view(direct.config, direct.db, **query)
    if args.json:
        _common.print_json(data)
    else:
        print_usage(data, args)
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(_usage(args))
    except ValueError as exc:
        if args.json:
            _common.print_json({"error": "invalid_usage_query", "detail": str(exc)})
        else:
            print(f"usage: {exc}", file=sys.stderr)
        return 2
