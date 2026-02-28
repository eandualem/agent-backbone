"""Dispatch data models."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DispatchResult:
    """Outcome of a dispatch operation."""

    delivered: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    offline: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
