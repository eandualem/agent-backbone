"""Shared helpers for SQL text-based database repositories."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any


def utc_now_iso() -> str:
    """Return the current UTC timestamp in the repository's canonical format."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def row_to_dict(row: Any) -> dict[str, Any] | None:
    """Convert a SQLAlchemy row to a plain dict when present."""
    if row is None:
        return None
    return dict(row._mapping)


def rows_to_dicts(rows: Sequence[Any]) -> list[dict[str, Any]]:
    """Convert SQLAlchemy row sequences to plain dictionaries."""
    return [dict(row._mapping) for row in rows]


def named_placeholders(
    prefix: str,
    values: Sequence[object],
) -> tuple[str, dict[str, object]]:
    """Build named SQL placeholders and matching params for IN clauses."""
    params = {f"{prefix}_{index}": value for index, value in enumerate(values)}
    placeholders = ", ".join(f":{name}" for name in params)
    return placeholders, params
