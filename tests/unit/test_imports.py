"""Every package must import cleanly in a fresh interpreter.

In-process tests share import state, so a circular import between two
packages only shows up when one of them is imported first — exactly what a
CLI entry point does. Spawn a fresh interpreter per entry module.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

_ENTRY_MODULES = [
    "agent_backbone",
    "agent_backbone.cli",
    "agent_backbone.api.app",
    "agent_backbone.services.terminal",
    "agent_backbone.services.agents",
    "agent_backbone.services.routing",
    "agent_backbone.services.infrastructure",
    "agent_backbone.services.github",
    "agent_backbone.services.telegram",
    "agent_backbone.services.database",
    "agent_backbone.hooks.claude_hook",
]


@pytest.mark.parametrize("module", _ENTRY_MODULES)
def test_module_imports_in_fresh_interpreter(module: str):
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
