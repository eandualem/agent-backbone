"""Automation service models."""

from __future__ import annotations

from dataclasses import dataclass, field

from collections.abc import Callable


@dataclass
class WorkflowEntry:
    """A discovered workflow template."""

    name: str
    description: str
    module: str = ""
    flow_fn: Callable | None = None
    source: str = "json"
    last_run: str | None = None
    steps: list[dict] = field(default_factory=list)
