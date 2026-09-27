"""``backbone signing owner``: who approves key changes for signed senders."""

from __future__ import annotations

import argparse
import asyncio

from agent_backbone.cli import _common
from agent_backbone.cli.presentation import note


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
