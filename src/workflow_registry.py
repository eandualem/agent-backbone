"""Workflow registry — discovers and catalogs workflow templates.

Scans flows/workflows/ for modules containing @flow-decorated functions.
Also discovers JSON-defined workflows from a configurable directory.
Provides listing and lookup by name for Telegram /workflow command and CLI.
"""

from __future__ import annotations

import importlib
import json
import logging
import pkgutil
from dataclasses import dataclass, field
from pathlib import Path

from prefect import Flow

import flows.workflows

log = logging.getLogger(__name__)


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


class WorkflowRegistry:
    """Discovers and provides access to workflow templates.

    Scans flows/workflows/ for modules with @flow-decorated callables.
    Optionally discovers JSON-defined workflows from a directory.
    """

    def __init__(self) -> None:
        self._workflows: dict[str, WorkflowEntry] = {}

    @property
    def workflows(self) -> dict[str, WorkflowEntry]:
        return dict(self._workflows)

    def _discover_prefect_flows(self) -> int:
        """Scan flows/workflows/ and register all @flow functions.

        Returns the number of Prefect workflows discovered.
        """
        count = 0
        package = flows.workflows
        package_path = package.__path__

        for importer, module_name, is_pkg in pkgutil.iter_modules(package_path):
            if is_pkg or module_name.startswith("_"):
                continue

            full_name = f"flows.workflows.{module_name}"
            try:
                mod = importlib.import_module(full_name)
            except Exception:
                log.warning("Failed to import workflow module %s", full_name, exc_info=True)
                continue

            # Find @flow-decorated callables in the module
            for attr_name in dir(mod):
                obj = getattr(mod, attr_name)
                if isinstance(obj, Flow):
                    entry = WorkflowEntry(
                        name=obj.name or module_name,
                        description=(obj.description or obj.fn.__doc__ or "").strip(),
                        module=full_name,
                        flow_fn=obj,
                        source="prefect",
                    )
                    self._workflows[entry.name] = entry
                    count += 1
                    log.debug("Registered workflow: %s from %s", entry.name, full_name)

        return count

    def discover_json_workflows(self, json_dir: Path) -> int:
        """Discover JSON-defined workflows from a directory.

        Each .json file defines one workflow with name, description, steps,
        and optional last_run timestamp.

        Returns the number of JSON workflows discovered.
        """
        count = 0
        if not json_dir.is_dir():
            return 0

        for path in sorted(json_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                log.warning("Failed to read workflow JSON: %s", path)
                continue

            name = data.get("name", path.stem)
            entry = WorkflowEntry(
                name=name,
                description=data.get("description", ""),
                source="json",
                last_run=data.get("last_run"),
                steps=data.get("steps", []),
            )
            self._workflows[entry.name] = entry
            count += 1
            log.debug("Registered JSON workflow: %s from %s", name, path)

        return count

    def discover(self, json_dir: Path | None = None) -> int:
        """Discover all workflows from Prefect flows and optionally JSON.

        Args:
            json_dir: Optional directory containing JSON workflow definitions.
                When None, only Prefect flows are discovered.

        Returns the total number of workflows discovered.
        """
        self._workflows.clear()
        count = self._discover_prefect_flows()
        if json_dir is not None:
            count += self.discover_json_workflows(json_dir)
        log.info("Discovered %d workflow(s)", count)
        return count

    def get(self, name: str) -> WorkflowEntry | None:
        """Look up a workflow by name."""
        return self._workflows.get(name)

    def list_names(self) -> list[str]:
        """Return sorted list of workflow names."""
        return sorted(self._workflows.keys())

    def format_list(self) -> str:
        """Format a human-readable list of available workflows."""
        if not self._workflows:
            return "No workflows registered."

        lines = ["Available workflows:"]
        for name in sorted(self._workflows):
            entry = self._workflows[name]
            desc = entry.description.split("\n")[0] if entry.description else "No description"
            lines.append(f"  {name}: {desc}")
        return "\n".join(lines)
