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


def format_iso(moment: datetime) -> str:
    """``moment`` (any timezone; naive means UTC) in the stored format."""
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC)
    return moment.strftime(_FORMAT)


def parse_iso(value: str) -> datetime:
    """A stored timestamp back to an aware UTC ``datetime`` (``ValueError`` if malformed)."""
    return datetime.strptime(value, _FORMAT).replace(tzinfo=UTC)
