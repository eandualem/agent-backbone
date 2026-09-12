"""Bounded incremental reads of local JSONL metadata; no transcripts are retained."""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from agent_backbone.usage import UsageEvent


@dataclass
class UsageBatch:
    offset: int
    state: dict
    events: list[UsageEvent] = field(default_factory=list)
    caught_up: bool = True
    error: str | None = None


def read_jsonl(
    path: Path, offset: int, state: dict, parse: Callable, *, budget: int = 8 * 1024 * 1024
) -> UsageBatch:
    """Only checkpoint complete lines; partial final writes are retried next read."""
    state = dict(state)
    batch = UsageBatch(offset, state)
    try:
        with path.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            identity = f"{stat.st_dev}:{stat.st_ino}"
            if offset > stat.st_size or state.get("_file", identity) != identity:
                session_id = state.get("_session_id")
                state.clear()
                if session_id:
                    state["_session_id"] = session_id
                state["partial"] = True
                offset = 0
            state["_file"] = identity
            stream.seek(offset)
            end = min(stat.st_size, offset + budget)
            while stream.tell() < end:
                start = stream.tell()
                line = stream.readline(budget + 1)
                if not line.endswith(b"\n"):
                    stream.seek(start)
                    batch.caught_up = False
                    if len(line) > budget:
                        batch.error = "source record exceeds read budget"
                    break
                try:
                    raw = json.loads(line)
                    if not isinstance(raw, dict):
                        raise ValueError("not an object")
                    draft = copy.deepcopy(state)
                    event = parse(raw, draft)
                    state.clear()
                    state.update(draft)
                    if event is not None:
                        batch.events.append(event)
                except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
                    state["partial"] = True
                batch.offset = stream.tell()
            batch.offset = stream.tell()
            batch.caught_up = batch.caught_up and batch.offset >= stat.st_size
    except OSError as exc:
        batch.error = type(exc).__name__
        batch.caught_up = False
    return batch


def count(data: dict, key: str) -> int:
    value = data.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("invalid token count")
    return value
