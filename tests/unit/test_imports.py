"""Every package must import cleanly in a fresh interpreter, and only from below.

In-process tests share import state, so a circular import between two
packages only shows up when one of them is imported first — exactly what a
CLI entry point does. Spawn a fresh interpreter per entry module and read
back which ``agent_backbone`` modules it pulled in: that is the layering
CLAUDE.md describes, made executable.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

_S = "agent_backbone.services."

# package -> packages it must never load (directly or transitively)
_FORBIDDEN: dict[str, tuple[str, ...]] = {
    "agent_backbone.config": (
        "agent_backbone.services",
        "agent_backbone.api",
        "agent_backbone.cli",
    ),
    _S + "terminal": (
        _S + "runtimes",
        _S + "agents",
        _S + "routing",
        _S + "jobs",
        _S + "database",
        _S + "github",
        _S + "integrations",
        _S + "swarm",
        "agent_backbone.api",
        "agent_backbone.cli",
        "agent_backbone.hooks",
        "agent_backbone.help",
    ),
    _S + "runtimes": (
        _S + "agents",
        _S + "routing",
        _S + "jobs",
        _S + "database",
        _S + "github",
        _S + "integrations",
        _S + "swarm",
        "agent_backbone.api",
        "agent_backbone.cli",
    ),
    _S + "database": (
        _S + "terminal",
        _S + "runtimes",
        _S + "agents",
        _S + "routing",
        _S + "jobs",
        _S + "github",
        _S + "integrations",
        _S + "swarm",
        "agent_backbone.api",
        "agent_backbone.cli",
    ),
    _S + "github": (
        _S + "terminal",
        _S + "runtimes",
        _S + "agents",
        _S + "routing",
        _S + "jobs",
        _S + "database",
        _S + "integrations",
        _S + "swarm",
        "agent_backbone.api",
        "agent_backbone.cli",
    ),
    _S + "agents": (
        _S + "routing",
        _S + "jobs",
        _S + "github",
        _S + "integrations",
        _S + "swarm",
        "agent_backbone.api",
        "agent_backbone.cli",
    ),
    _S + "routing": (
        _S + "jobs",
        _S + "integrations",
        _S + "swarm",
        "agent_backbone.api",
        "agent_backbone.cli",
    ),
    _S + "integrations": (_S + "jobs", _S + "swarm", "agent_backbone.api", "agent_backbone.cli"),
    _S + "integrations.telegram": (
        _S + "jobs",
        _S + "swarm",
        "agent_backbone.api",
        "agent_backbone.cli",
    ),
    _S + "jobs": (_S + "swarm", "agent_backbone.api", "agent_backbone.cli"),
    _S + "swarm": (_S + "jobs", "agent_backbone.api", "agent_backbone.cli"),
    "agent_backbone.hooks.claude_hook": ("agent_backbone.services", "agent_backbone.config"),
    "agent_backbone.api.app": (),
    "agent_backbone.cli": (),
}


def _loaded(module: str) -> list[str]:
    code = (
        f"import {module}, sys; "
        "print('\\n'.join(sorted(m for m in sys.modules if m.startswith('agent_backbone'))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.split()


@pytest.mark.parametrize("module", sorted(_FORBIDDEN))
def test_package_imports_only_from_below(module: str):
    loaded = _loaded(module)
    offenders = [
        m for m in loaded if any(m == f or m.startswith(f + ".") for f in _FORBIDDEN[module])
    ]
    assert not offenders, f"{module} loads packages above it: {offenders}"
