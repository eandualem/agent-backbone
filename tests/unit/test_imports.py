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
from pathlib import Path

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


# The layering CLAUDE.md describes, as ranks: a module may import a package
# of the same or a lower rank, never a higher one. Function-local imports
# count (they are still edges); imports under ``if TYPE_CHECKING:`` do not.
_RANK: dict[str, int] = {
    "__init__": 0,
    "base": 0,
    "config": 0,
    "fs": 0,
    "git": 0,
    "help": 0,
    "hooks": 0,
    "models": 0,
    "recent": 0,
    "release": 0,
    "templates": 0,
    "services": 0,  # the package itself
    "services.scheduler": 1,
    "services.terminal": 1,
    "services.runtimes": 2,
    "services.database": 2,
    "services.github": 2,
    "services.agents": 3,
    "services.routing": 4,
    "services.integrations": 5,
    "services.jobs": 6,
    "services.swarm": 6,
    "api": 7,
    "cli": 8,
}


def _package_of(module: str) -> str:
    """``…services.jobs.retry`` -> ``services.jobs``; ``agent_backbone.fs`` -> ``fs``."""
    parts = module.split(".")
    assert parts[0] == "agent_backbone", module
    if len(parts) == 1:
        return "__init__"
    if parts[1] == "services":
        return "services." + parts[2] if len(parts) > 2 else "services"
    return parts[1]


def _import_edges(path: Path, module: str) -> list[tuple[int, str]]:
    """``(line, imported module)`` for every runtime import of ``agent_backbone.*``."""
    import ast

    tree = ast.parse(path.read_text(), filename=str(path))
    type_checking: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = node.test
            name = test.id if isinstance(test, ast.Name) else getattr(test, "attr", "")
            if name == "TYPE_CHECKING":
                for inner in ast.walk(node):
                    type_checking.add(id(inner))
    edges: list[tuple[int, str]] = []
    package = module.rsplit(".", 1)[0] if not path.name == "__init__.py" else module
    for node in ast.walk(tree):
        if id(node) in type_checking:
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("agent_backbone"):
                    edges.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                target = f"{base}.{node.module}" if node.module else base
            else:
                target = node.module or ""
            if target.startswith("agent_backbone"):
                edges.append((node.lineno, target))
    return edges


def test_every_module_imports_only_from_its_layer_or_below():
    """Static: every ``agent_backbone`` import edge, function-local ones included."""
    root = Path(__file__).resolve().parents[2] / "src" / "agent_backbone"
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).with_suffix("")
        parts = rel.parent.parts if rel.name == "__init__" else rel.parts
        module = "agent_backbone" + ("." + ".".join(parts) if parts else "")
        source = _package_of(module)
        for line, target in _import_edges(path, module):
            dest = _package_of(target)
            assert source in _RANK, f"{source} has no rank in test_imports._RANK"
            assert dest in _RANK, f"{dest} has no rank in test_imports._RANK"
            if _RANK[dest] > _RANK[source]:
                where = f"{path.relative_to(root)}:{line}"
                violations.append(f"{where} imports {target} ({source} -> {dest})")
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize("module", sorted(_FORBIDDEN))
def test_package_imports_only_from_below(module: str):
    loaded = _loaded(module)
    offenders = [
        m for m in loaded if any(m == f or m.startswith(f + ".") for f in _FORBIDDEN[module])
    ]
    assert not offenders, f"{module} loads packages above it: {offenders}"
