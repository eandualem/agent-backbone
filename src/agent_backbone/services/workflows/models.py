"""Workflow service models."""

from __future__ import annotations

from dataclasses import dataclass, field

from prefect import Flow


@dataclass
class WorkflowEntry:
    """A discovered workflow template."""

    name: str
    description: str
    module: str = ""
    flow_fn: Flow | None = None
    source: str = "prefect"  # "prefect" or "json"
    last_run: str | None = None
    steps: list[dict] = field(default_factory=list)
