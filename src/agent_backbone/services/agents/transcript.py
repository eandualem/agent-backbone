"""What an agent has been doing: a bounded tail of its runtime's own
conversation record (``backbone agent output``), or the screen when the
runtime keeps none.

The transcript is located from the session id the runtime's hook recorded
(only when that record's runtime is the live one) through the runtime's
``usage_paths``; the runtime parses its own format. Reads are bounded to
the tail of the file and to a number of entries: never a whole session.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent_backbone.services.agents._file_reader import read_state_file
from agent_backbone.services.runtimes import get_runtime, sanitize_pane_content
from agent_backbone.services.terminal import capture_pane, query_environment_var, session_exists

if TYPE_CHECKING:
    from pathlib import Path

    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.runtimes import Runtime

DEFAULT_ENTRIES = 40
MAX_ENTRIES = 500
TAIL_BYTES = 256 * 1024
"""One read step backwards through the file (a forward read from a cursor
reads at most this much)."""
MAX_TAIL_BYTES = 4 * 1024 * 1024
"""How far back a tail read may look in total: bounded recent content, never
the whole session (Codex records can carry whole command outputs)."""


@dataclass(frozen=True)
class OutputTail:
    """The lines, where they came from and how to continue from here."""

    session: str
    source: str
    """``transcript`` (the runtime's own record) or ``screen`` (``capture-pane``)."""
    runtime: str
    lines: list[str] = field(default_factory=list)
    cursor: int | None = None
    """Byte offset after the last transcript record returned; None for the screen."""
    evidence: list[str] = field(default_factory=list)


def locate_transcript(
    config: BackboneConfig, name: str, runtime_id: str | None
) -> tuple[Path | None, Runtime, list[str]]:
    """The transcript file for the agent's live session, its runtime and why not."""
    spec = config.agents.get(name)
    last = read_state_file(config.state_dir, name)
    runtime_id = runtime_id or (last.runtime if last is not None else None)
    if not runtime_id and spec is not None:
        runtime_id = spec.runtime
    rt = get_runtime(runtime_id)
    if not rt.transcript_supported:
        return None, rt, [f"{rt.id} keeps no transcript the backbone can read"]
    if last is None or not last.session_id:
        return None, rt, ["no session id recorded by the hook yet"]
    if last.runtime and last.runtime != rt.id:
        return None, rt, [f"the recorded session id belongs to {last.runtime}, not {rt.id}"]
    env = dict(spec.env) if spec is not None else {}
    paths = [p for p in rt.usage_paths(last.session_id, env) if p.is_file()]
    if not paths:
        return None, rt, [f"no transcript file found for session {last.session_id}"]
    newest = max(paths, key=lambda p: p.stat().st_mtime)
    return newest, rt, [f"transcript {newest}"]


def read_transcript_tail(
    path: Path, rt: Runtime, *, lines: int, since: int | None
) -> tuple[list[str], int]:
    """The last ``lines`` entries (or every entry after byte ``since``, up to
    ``lines``) and the byte offset to continue from."""
    size = path.stat().st_size
    if since is not None:
        start = min(max(since, 0), size)
        with path.open("rb") as stream:
            stream.seek(start)
            raw = stream.read(TAIL_BYTES)
        end = start + len(raw)
        if raw and not raw.endswith(b"\n") and end < size:
            raw, _, partial = raw.rpartition(b"\n")
            raw += b"\n"
            end -= len(partial)
        entries = rt.transcript_entries(_records(raw))[:lines]
    else:
        # Walk backwards a step at a time until there are enough entries or
        # the look-back bound is reached; the first record of a step that did
        # not start at the file's beginning is cut mid-line and dropped.
        start = size
        entries = []
        with path.open("rb") as stream:
            while start > 0 and len(entries) < lines and size - start < MAX_TAIL_BYTES:
                start = max(0, start - TAIL_BYTES)
                stream.seek(start)
                raw = stream.read(size - start)
                if start:
                    raw = raw.partition(b"\n")[2]
                entries = rt.transcript_entries(_records(raw))
        entries = entries[-lines:]
        end = size
    rendered = [f"{entry.time} {entry.role}: {entry.text}".strip() for entry in entries]
    return rendered, end


def _records(raw: bytes) -> list[dict]:
    records: list[dict] = []
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


async def output_tail(
    config: BackboneConfig,
    name: str,
    *,
    lines: int = DEFAULT_ENTRIES,
    since: int | None = None,
    screen: bool = False,
) -> OutputTail:
    """Recent activity of a registered agent: its transcript, else its screen."""
    lines = max(1, min(lines, MAX_ENTRIES))
    online = await session_exists(name)
    live_runtime = await query_environment_var(name, "BACKBONE_RUNTIME") if online else None
    evidence: list[str] = []
    if not screen:
        path, rt, why = locate_transcript(config, name, live_runtime)
        evidence.extend(why)
        if path is not None:
            try:
                rendered, cursor = await asyncio.to_thread(
                    read_transcript_tail, path, rt, lines=lines, since=since
                )
            except OSError as exc:
                evidence.append(f"transcript unreadable: {type(exc).__name__}")
            else:
                return OutputTail(name, "transcript", rt.id, rendered, cursor, evidence)
    else:
        evidence.append("screen requested")
    if not online:
        return OutputTail(name, "screen", live_runtime or "", [], None, [*evidence, "offline"])
    pane = await capture_pane(name, lines=lines)
    text = sanitize_pane_content(pane)
    visible = [line.rstrip() for line in text.splitlines()]
    while visible and not visible[-1]:
        visible.pop()
    spec = config.agents.get(name)
    runtime_id = live_runtime or (spec.runtime if spec is not None else "")
    return OutputTail(name, "screen", runtime_id, visible[-lines:], None, evidence)


__all__ = ["DEFAULT_ENTRIES", "MAX_ENTRIES", "OutputTail", "output_tail", "read_transcript_tail"]
