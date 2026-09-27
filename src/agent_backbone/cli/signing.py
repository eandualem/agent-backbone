"""``backbone signing owner``: who approves key changes for signed senders;
``backbone message validate``: an agent's claim on an owner confirmation."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from agent_backbone.cli import _common
from agent_backbone.cli.presentation import note, print_record


def cmd_signing(args: argparse.Namespace) -> int:
    return asyncio.run(_owner(args))


async def _owner(args: argparse.Namespace) -> int:
    boot = await _common.read_client_config()
    result = await _common.api(
        boot, "POST", "/api/signing/owner", json_body={"telegram_user_id": args.user_id}
    )
    if result is None:
        print(_common.unreachable())
        return 1
    status, data = result
    if args.json:
        _common.print_json(data)
        return 0 if status == 200 else 1
    if status != 200:
        print(f"error {status}: {data}")
        return 1
    if data["outcome"] == "set":
        note(f"Key changes are now approved from Telegram user {args.user_id}.")
    else:
        note(
            f"Requested. The current owner approves it in Telegram with "
            f"/approve_owner {args.user_id}."
        )
    return 0


def cmd_message(args: argparse.Namespace) -> int:
    return asyncio.run(_validate(args))


def _when(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="seconds")


async def _validate(args: argparse.Namespace) -> int:
    boot = await _common.read_client_config()
    result = await _common.api(
        boot,
        "POST",
        "/api/messages/validate",
        json_body={"confirmation_id": args.confirmation_id, "done": args.done},
    )
    if result is None:
        print(f"{_common.unreachable()}. The confirmation could not be validated.")
        return 1
    status, data = result
    if args.json:
        _common.print_json(data)
        return 0 if status == 200 else 1
    if status != 200:
        detail = data.get("detail")
        if isinstance(detail, dict) and "reason" in detail:
            print(f"Not validated ({detail['reason']}): {detail.get('message', '')}")
        else:
            print(f"error {status}: {detail}")
        return 1
    if data["outcome"] == "closed":
        note(f"Closed. The claim on {args.confirmation_id} no longer grants anything.")
        return 0
    claim = (
        "recovered: the same claim as before, not fresh authority" if data["recovered"] else "new"
    )
    print_record(
        "Owner-confirmed message",
        [
            ("Confirmation", data["confirmation_id"]),
            ("From", data["sender"]),
            ("Kind", data["kind"]),
            ("Confirmed", f"{data['confirmed_at']} ({data['source']})"),
            ("Claim", claim),
            ("Expires", f"{_when(data['grant']['expires_at'])}, or when closed with --done"),
        ],
    )
    print("Confirmed text (terminal controls shown escaped; --json gives it exactly):")
    shown = _escaped(data["text"])
    print(shown, end="" if shown.endswith("\n") else "\n")
    return 0


def _escaped(text: str) -> str:
    """The text with terminal controls written as escapes; line breaks and tabs kept."""
    return "".join(
        c if c in "\n\t" or c.isprintable() else c.encode("unicode_escape").decode() for c in text
    )
