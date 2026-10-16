#!/usr/bin/env python3
"""Chrome native messaging host for the optional tab-group names extension.

Chrome starts it for each request, sends one length-prefixed JSON message on
stdin and reads one reply from stdout. The reply lists the tab group each
Backbone agent's Claude-in-Chrome sessions used, as the Claude hook recorded
them in ``<state_dir>/chrome-groups/<agent>.<group>.json``, with the title to
give each.
It reads nothing else and needs no credentials.

Standard library only: Chrome runs it outside any virtual environment.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import time
from pathlib import Path

MAX_AGE_SECONDS = 7 * 24 * 3600
"""Records older than this are from long-closed sessions; their groups are gone."""


def title_for(agent: str) -> str:
    """``docs-writer`` → ``Docs Writer``."""
    words = agent.replace("_", " ").replace("-", " ").split()
    return " ".join(word[:1].upper() + word[1:] for word in words) or agent


def groups(state_dir: Path, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    found: dict[int, dict | None] = {}
    for path in sorted((state_dir / "chrome-groups").glob("*.json")):
        try:
            record = json.loads(path.read_text())
            group, seen = int(record["group"]), float(record["ts"])
            if not isinstance(record["tabs"], list):
                raise TypeError("tabs must be a list")  # "5" would read as tab 5
            tab_ids = [int(tab) for tab in record["tabs"]]
            agent = str(record["agent"])
        except (OSError, ValueError, KeyError, TypeError):
            continue  # one unreadable record must not cost the others their names
        if now - seen > MAX_AGE_SECONDS:
            continue
        # Two agents naming one group: neither name is proven, so it keeps its title.
        entry = {"group": group, "tabs": tab_ids, "title": title_for(agent)}
        found[group] = None if group in found else entry
    return [entry for entry in found.values() if entry is not None]


def read_message(stream) -> dict | None:
    header = stream.read(4)
    if len(header) < 4:
        return None
    (length,) = struct.unpack("=I", header)
    return json.loads(stream.read(length) or b"{}")


def write_message(stream, message: dict) -> None:
    body = json.dumps(message).encode()
    stream.write(struct.pack("=I", len(body)) + body)
    stream.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--state-dir", required=True)
    args, _chrome_args = parser.parse_known_args(argv)  # Chrome appends its origin
    if read_message(sys.stdin.buffer) is None:
        return 0
    write_message(sys.stdout.buffer, {"groups": groups(Path(args.state_dir).expanduser())})
    return 0


if __name__ == "__main__":
    sys.exit(main())
