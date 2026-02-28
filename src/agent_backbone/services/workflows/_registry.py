"""Workflow registry — discovers and catalogs workflow templates.

Scans the _flows module for @flow-decorated functions.
Also discovers JSON-defined workflows from a configurable directory.
Provides listing and lookup by name for Telegram /workflow command and CLI.
"""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path

from prefect import Flow

from agent_backbone.services.workflows.models import WorkflowEntry

log = logging.getLogger(__name__)

_FLOWS_MODULE = "agent_backbone.services.workflows._flows"


class WorkflowRegistry:
    """Discovers and provides access to workflow templates.

    Scans the _flows module for @flow-decorated callables.
    Optionally discovers JSON-defined workflows from a directory.
    """

    def __init__(self) -> None:
        self._workflows: dict[str, WorkflowEntry] = {}

    @property
    def workflows(self) -> dict[str, WorkflowEntry]:
        return dict(self._workflows)

    def _discover_prefect_flows(self) -> int:
        """Scan _flows module and register all @flow functions.

        Returns the number of Prefect workflows discovered.
        """
        count = 0

        try:
            mod = importlib.import_module(_FLOWS_MODULE)
        except Exception:
            log.warning("Failed to import workflow flows module %s", _FLOWS_MODULE, exc_info=True)
            return 0

        # Find @flow-decorated callables in the module
        for attr_name in dir(mod):
            obj = getattr(mod, attr_name)
            if isinstance(obj, Flow):
                entry = WorkflowEntry(
                    name=obj.name or attr_name,
                    description=(obj.description or obj.fn.__doc__ or "").strip(),
                    module=_FLOWS_MODULE,
                    flow_fn=obj,
                    source="prefect",
                )
                self._workflows[entry.name] = entry
                count += 1
                log.debug("Registered workflow: %s from %s", entry.name, _FLOWS_MODULE)

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
