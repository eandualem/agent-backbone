"""Timestamps as stored in the database: ISO 8601, UTC, microseconds, ``Z`` suffix.

Every column that holds a time is ``Text`` in this format, so rows compare
lexically in ``WHERE created_at < :cutoff`` on SQLite and PostgreSQL alike.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def now_iso() -> str:
    return datetime.now(UTC).strftime(_FORMAT)


def cutoff_iso(**delta) -> str:
    """The timestamp ``timedelta(**delta)`` ago, e.g. ``cutoff_iso(minutes=5)``."""
    return (datetime.now(UTC) - timedelta(**delta)).strftime(_FORMAT)
