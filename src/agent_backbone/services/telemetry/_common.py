"""Shared helpers for native telemetry adapters."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


def parse_timestamp(value: object) -> float:
    """Parse runtime-specific timestamps into POSIX seconds."""
    if isinstance(value, int | float):
        timestamp = float(value)
        return timestamp / 1000.0 if timestamp > 10_000_000_000 else timestamp
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        timestamp = float(value)
        return timestamp / 1000.0 if timestamp > 10_000_000_000 else timestamp
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def event_id(base: str, suffix: str) -> str:
    """Build a stable derived event identifier."""
    return f"{base}:{suffix}"


def simplify_value(value: object, *, depth: int = 0) -> object:
    """Recursively normalize runtime payloads for storage."""
    if depth >= 4:
        return str(value)
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, list):
        return [simplify_value(item, depth=depth + 1) for item in value[:50]]
    if isinstance(value, dict):
        simplified: dict[str, object] = {}
        for key, item in value.items():
            if key in {"encrypted_content", "base_instructions"}:
                continue
            simplified[str(key)] = simplify_value(item, depth=depth + 1)
        return simplified
    return str(value)


def read_jsonl_since(
    path: Path,
    checkpoint: dict[str, object] | None,
) -> tuple[list[tuple[int, dict[str, object]]], dict[str, object]]:
    """Read JSONL records after the stored byte offset checkpoint."""
    offset = int(checkpoint.get("offset", 0)) if checkpoint else 0
    if not path.exists():
        return [], {"offset": 0}

    size = path.stat().st_size
    if offset < 0 or offset > size:
        offset = 0

    records: list[tuple[int, dict[str, object]]] = []
    with path.open("r", encoding="utf-8") as handle:
        handle.seek(offset)
        while True:
            line_offset = handle.tell()
            line = handle.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                log.debug("Skipping malformed telemetry line in %s at offset %s", path, line_offset)
                continue
            if isinstance(payload, dict):
                records.append((line_offset, payload))
        new_offset = handle.tell()
    return records, {"offset": new_offset}


def read_text_since(
    path: Path,
    checkpoint: dict[str, object] | None,
) -> tuple[str, dict[str, object]]:
    """Read a text-file delta after the stored byte offset checkpoint."""
    offset = int(checkpoint.get("offset", 0)) if checkpoint else 0
    if not path.exists():
        return "", {"offset": 0}

    size = path.stat().st_size
    if offset < 0 or offset > size:
        offset = 0

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(offset)
        payload = handle.read()
        new_offset = handle.tell()
    return payload, {"offset": new_offset}


def normalize_claude_content(content: object) -> list[dict[str, object]]:
    """Normalize Claude transcript content blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return []
    parts: list[dict[str, object]] = []
    for item in content:
        if isinstance(item, dict):
            parts.append(simplify_value(item))  # type: ignore[arg-type]
    return parts


def normalize_codex_content(content: object) -> list[dict[str, object]]:
    """Normalize Codex content blocks."""
    if not isinstance(content, list):
        return []
    parts: list[dict[str, object]] = []
    for item in content:
        if isinstance(item, dict):
            parts.append(simplify_value(item))  # type: ignore[arg-type]
    return parts


def extract_text_parts(content: object) -> list[str]:
    """Extract plain text fragments from content arrays."""
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    values: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            values.append(text)
    return values


def cursor_project_slug(cwd: Path) -> str:
    """Build the Cursor project directory slug for a cwd."""
    return str(cwd).lstrip("/").replace("/", "-")


def mtime(path: Path) -> float:
    """Return filesystem modification time for sorting."""
    return path.stat().st_mtime
