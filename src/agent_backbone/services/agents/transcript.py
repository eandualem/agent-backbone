"""What an agent has been saying: its user-facing messages, complete, out of
its runtime's own conversation record (``backbone agent output``), or the
screen when the runtime keeps none.

The transcript is located from the session id the runtime's hook recorded
(only when that record's runtime is the live one) through the runtime's
``usage_paths``; the runtime parses its own format and returns only the
messages the agent addressed to the person, never shortened. Navigation is
by byte offset into the append-only file: a page is a bounded number of
messages, read backwards from the end (or from ``before``) or forwards from
``since`` (optionally up to ``end``); every page says whether more lies
before or after it, so nothing is silently dropped.
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
    from agent_backbone.services.runtimes import Runtime, TranscriptEntry

DEFAULT_MESSAGES = 20
MAX_MESSAGES = 200
STEP_BYTES = 1024 * 1024
"""One read step through the file."""
MAX_SCAN_BYTES = 64 * 1024 * 1024
"""How much of the file one page read may scan for messages (Codex records
can carry whole command outputs between two messages); a page that hits
this bound says so with ``more_before``/``more_after`` and its evidence."""


@dataclass(frozen=True)
class OutputPage:
    """One page of messages (or the screen), with how to reach the rest."""

    session: str
    source: str
    """``transcript`` (the runtime's own record) or ``screen`` (``capture-pane``)."""
    runtime: str
    messages: list[TranscriptEntry] = field(default_factory=list)
    """Complete user-facing messages, oldest first (transcript source)."""
    range_start: int | None = None
    """Byte offset of the first message on the page; ``--before`` it to go back."""
    range_end: int | None = None
    """Byte offset after the last message; ``--since`` it to continue."""
    more_before: bool = False
    more_after: bool = False
    lines: list[str] = field(default_factory=list)
    """The visible terminal, ANSI stripped (screen source)."""
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
    stamped = []
    for p in rt.usage_paths(last.session_id, env):
        try:
            if p.is_file():
                stamped.append((p.stat().st_mtime, p))
        except OSError:  # archived or removed while we looked
            continue
    if not stamped:
        return None, rt, [f"no transcript file found for session {last.session_id}"]
    newest = max(stamped, key=lambda item: item[0])[1]
    return newest, rt, [f"transcript {newest}"]


def _records(raw: bytes, base: int) -> list[dict]:
    """Parsed JSON records of ``raw`` (which starts at file offset ``base``),
    each stamped with its ``_start``/``_end`` byte offsets."""
    records: list[dict] = []
    offset = base
    lines = raw.split(b"\n")
    for index, line in enumerate(lines):
        length = len(line) + (1 if index < len(lines) - 1 else 0)
        if line.strip():
            try:
                record = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                record = None
            if isinstance(record, dict):
                record["_start"] = offset
                record["_end"] = offset + length
                records.append(record)
        offset += length
    return records


def read_messages(
    path: Path,
    rt: Runtime,
    *,
    limit: int,
    since: int | None = None,
    before: int | None = None,
    end: int | None = None,
) -> tuple[list[TranscriptEntry], bool, bool, list[str]]:
    """A page of complete messages and whether more exist before and after it.

    Backwards (the default, or from ``before``): the last ``limit`` messages
    ending at or before that offset. Forwards (``since``): the first ``limit``
    messages starting at or after it, up to ``end``. Returns
    ``(messages, more_before, more_after, evidence)``."""
    size = path.stat().st_size
    evidence: list[str] = []
    with path.open("rb") as stream:
        if since is not None:
            pos = min(max(since, 0), size)
            stop = min(max(end, pos), size) if end is not None else size
            collected: list[TranscriptEntry] = []
            scanned = 0
            while pos < stop and len(collected) <= limit and scanned < MAX_SCAN_BYTES:
                chunk = _forward_chunk(stream, pos, stop)
                collected.extend(rt.transcript_entries(_records(chunk, pos)))
                pos += len(chunk)
                scanned += len(chunk)
            messages = collected[:limit]
            more_after = len(collected) > limit or pos < stop
            if pos < stop and len(collected) <= limit:
                evidence.append(f"scan bound reached at offset {pos}; continue with --since {pos}")
            more_before = since > 0
            return messages, more_before, more_after, evidence
        stop = min(max(before, 0), size) if before is not None else size
        pos = stop
        collected = []
        scanned = 0
        while pos > 0 and len(collected) <= limit and scanned < MAX_SCAN_BYTES:
            start, chunk = _backward_chunk(stream, pos)
            collected = rt.transcript_entries(_records(chunk, start)) + collected
            scanned += pos - start
            pos = start
        messages = collected[-limit:]
        more_before = len(collected) > limit or pos > 0
        if pos > 0 and len(collected) <= limit:
            evidence.append(f"scan bound reached at offset {pos}; go back with --before {pos}")
        more_after = stop < size
        return messages, more_before, more_after, evidence


def _forward_chunk(stream, pos: int, stop: int) -> bytes:
    """Whole records from ``pos`` towards ``stop``: about one step, extended
    as far as needed to end on a record boundary (a record may be longer
    than a step)."""
    stream.seek(pos)
    chunk = stream.read(min(STEP_BYTES, stop - pos))
    while pos + len(chunk) < stop:
        cut = chunk.rfind(b"\n")
        if cut >= 0:
            return chunk[: cut + 1]
        if len(chunk) >= MAX_SCAN_BYTES:
            raise ValueError(f"a record at offset {pos} exceeds the scan bound")
        chunk += stream.read(min(STEP_BYTES, stop - pos - len(chunk)))
    return chunk


def _backward_chunk(stream, pos: int) -> tuple[int, bytes]:
    """Whole records ending at ``pos``: about one step back, extended as far
    as needed to begin on a record boundary. Returns ``(start, chunk)``."""
    start = max(0, pos - STEP_BYTES)
    while True:
        stream.seek(start)
        chunk = stream.read(pos - start)
        if start == 0:
            return 0, chunk
        head, sep, rest = chunk.partition(b"\n")
        if sep and rest:  # the window holds at least one whole record
            return start + len(head) + len(sep), rest
        if pos - start >= MAX_SCAN_BYTES:
            raise ValueError(f"a record ending at offset {pos} exceeds the scan bound")
        start = max(0, start - STEP_BYTES)  # only the tail of a long record: widen


async def output_page(
    config: BackboneConfig,
    name: str,
    *,
    limit: int = DEFAULT_MESSAGES,
    since: int | None = None,
    before: int | None = None,
    end: int | None = None,
    screen: bool = False,
) -> OutputPage:
    """A page of a registered agent's messages, else its screen."""
    limit = max(1, min(limit, MAX_MESSAGES))
    online = await session_exists(name)
    live_runtime = await query_environment_var(name, "BACKBONE_RUNTIME") if online else None
    evidence: list[str] = []
    if not screen:
        path, rt, why = locate_transcript(config, name, live_runtime)
        evidence.extend(why)
        if path is not None:
            try:
                messages, more_before, more_after, notes = await asyncio.to_thread(
                    read_messages, path, rt, limit=limit, since=since, before=before, end=end
                )
            except OSError as exc:
                evidence.append(f"transcript unreadable: {type(exc).__name__}")
            except ValueError as exc:
                evidence.append(f"transcript unreadable: {exc}")
            else:
                return OutputPage(
                    name,
                    "transcript",
                    rt.id,
                    messages,
                    messages[0].start if messages else (since if since is not None else before),
                    messages[-1].end if messages else (since if since is not None else before),
                    more_before,
                    more_after,
                    [],
                    [*evidence, *notes],
                )
    else:
        evidence.append("screen requested")
    if not online:
        return OutputPage(name, "screen", live_runtime or "", evidence=[*evidence, "offline"])
    pane = await capture_pane(name, lines=limit)
    text = sanitize_pane_content(pane)
    visible = [line.rstrip() for line in text.splitlines()]
    while visible and not visible[-1]:
        visible.pop()
    spec = config.agents.get(name)
    runtime_id = live_runtime or (spec.runtime if spec is not None else "")
    return OutputPage(name, "screen", runtime_id, lines=visible[-limit:], evidence=evidence)


__all__ = ["DEFAULT_MESSAGES", "MAX_MESSAGES", "OutputPage", "output_page", "read_messages"]
