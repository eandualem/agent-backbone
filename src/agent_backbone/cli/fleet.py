"""``backbone fleet …`` — save the running agents, resume their exact conversations."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re

from agent_backbone.cli import _common
from agent_backbone.cli.presentation import note, print_record, print_table

_RESUME_TIMEOUT = 1800.0
"""A fleet resume waits for every agent's prompt, a few at a time."""


def _snapshot_ref(value: str) -> str:
    if value != "latest" and not re.fullmatch(r"[1-9][0-9]*", value):
        raise argparse.ArgumentTypeError("a snapshot is a number or 'latest'")
    return value


def add_fleet_parser(sub) -> None:
    p = sub.add_parser(
        "fleet", help="save the running agents and resume their exact conversations later"
    )
    fsub = p.add_subparsers(dest="fleet_command", required=True)
    save = fsub.add_parser("save", help="save every running agent and its conversation")
    save.add_argument("--stop", action="store_true", help="then stop the saved agents")
    save.add_argument(
        "--force", action="store_true", help="with --stop, also stop busy or waiting agents"
    )
    save.add_argument("--note", default=None, help="a note kept with the snapshot")
    fsub.add_parser("list", help="saved snapshots, newest first")
    show = fsub.add_parser("show", help="one snapshot and its resumes")
    show.add_argument("snapshot", nargs="?", default="latest", type=_snapshot_ref)
    resume = fsub.add_parser(
        "resume", help="start each saved agent on exactly its saved conversation"
    )
    resume.add_argument("snapshot", nargs="?", default="latest", type=_snapshot_ref)
    for parser in (save, fsub.choices["list"], show, resume):
        parser.add_argument("--json", action="store_true", help="print structured data")
    p.set_defaults(func=cmd_fleet)


def _print_snapshot(data: dict) -> None:
    counts = data.get("counts", {})
    print_record(
        f"Fleet snapshot {data['id']}",
        [
            ("Saved", data.get("created_at")),
            ("By", data.get("created_by") or "-"),
            ("Note", data.get("note") or "-"),
            ("Agents", f"{counts.get('saved', 0)} saved, {counts.get('resumable', 0)} resumable"),
        ],
    )
    print_table(
        "Agents",
        ("Agent", "CLI", "State", "Stop", "Resumable"),
        [
            (
                entry["name"],
                entry["runtime"],
                entry["state_at_save"],
                entry["stop"] + (f": {entry['stop_error']}" if entry.get("stop_error") else ""),
                "yes" if entry["resumable"] else f"no: {entry['not_resumable_reason']}",
            )
            for entry in data.get("agents", [])
        ],
    )
    if counts.get("skipped_busy"):
        note(
            f"{counts['skipped_busy']} busy or waiting agent(s) left running: "
            "backbone fleet save --stop --force stops them too"
        )


def _print_run(run: dict) -> None:
    print_table(
        f"Resume of snapshot {run['snapshot_id']}",
        ("Agent", "Outcome", "Ready", "Session"),
        [
            (
                entry["name"],
                entry["outcome"] + (f": {entry['reason']}" if entry.get("reason") else ""),
                entry.get("ready") or "-",
                {True: "confirmed", False: "different id reported", None: "-"}[
                    entry.get("session_confirmed")
                ],
            )
            for entry in run.get("agents", [])
        ],
    )
    for entry in run.get("agents", []):
        if entry["outcome"] in ("not_resumed", "failed"):
            for line in entry.get("evidence", []):
                note(f"{entry['name']}: {line}")


async def _fleet(args: argparse.Namespace) -> int:
    boot = await _common.read_client_config()
    sub = args.fleet_command
    me = os.environ.get("BACKBONE_AGENT", "").strip()
    if sub == "save":
        if args.force and not args.stop:
            print("--force applies to --stop")
            return 1
        body = {"stop": args.stop, "force": args.force, "note": args.note, "from_entity": me}
        result = await _common.api(
            boot, "POST", "/api/fleet/snapshots", json_body=body, timeout=300.0
        )
    elif sub == "list":
        result = await _common.api(boot, "GET", "/api/fleet/snapshots")
    elif sub == "show":
        result = await _common.api(boot, "GET", f"/api/fleet/snapshots/{args.snapshot}")
    else:
        result = await _common.api(
            boot,
            "POST",
            f"/api/fleet/snapshots/{args.snapshot}/resume",
            json_body={"from_entity": me},
            timeout=_RESUME_TIMEOUT,
        )
    if result is None:
        print(_common.unreachable())
        return 1
    status, data = result
    if status not in (200, 201):
        print(f"error {status}: {data.get('detail') if isinstance(data, dict) else data}")
        return 1
    if args.json:
        print(json.dumps(data, indent=2))
    elif sub == "list":
        print_table(
            "Fleet snapshots",
            ("Snapshot", "Saved", "By", "Agents", "Last resume"),
            [
                (
                    item["id"],
                    item["created_at"],
                    item.get("created_by") or "-",
                    item["counts"].get("saved", 0),
                    (item.get("last_resume") or {}).get("at") or "never",
                )
                for item in data
            ],
            empty="No fleet snapshots yet: backbone fleet save",
        )
    elif sub == "resume":
        _print_run(data)
    else:
        _print_snapshot(data)
        if sub == "show" and data.get("resumes"):
            _print_run(data["resumes"][-1])
    if sub == "resume":
        return 0 if all(e["outcome"] != "failed" for e in data.get("agents", [])) else 1
    return 0


def cmd_fleet(args: argparse.Namespace) -> int:
    return asyncio.run(_fleet(args))
