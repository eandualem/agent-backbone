"""Workflow registry — discovers and catalogs workflow templates.

Scans flows/workflows/ for modules containing @flow-decorated functions.
Provides listing and lookup by name for Telegram /workflow command and CLI.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from dataclasses import dataclass

from prefect import Flow

import flows.workflows

log = logging.getLogger(__name__)


@dataclass
class WorkflowEntry:
    """A discovered workflow template."""

    name: str
    description: str
    module: str
    flow_fn: Flow


class WorkflowRegistry:
    """Discovers and provides access to workflow templates.

    Scans flows/workflows/ for modules with @flow-decorated callables.
    """

    def __init__(self) -> None:
        self._workflows: dict[str, WorkflowEntry] = {}

    @property
    def workflows(self) -> dict[str, WorkflowEntry]:
        return dict(self._workflows)

    def discover(self) -> int:
        """Scan flows/workflows/ and register all @flow functions.

        Returns the number of workflows discovered.
        """
        self._workflows.clear()
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
                    )
                    self._workflows[entry.name] = entry
                    log.debug("Registered workflow: %s from %s", entry.name, full_name)

        log.info("Discovered %d workflow(s)", len(self._workflows))
        return len(self._workflows)

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
