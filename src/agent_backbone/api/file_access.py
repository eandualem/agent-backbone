"""Shared helpers for restricted file browsing and editing endpoints."""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from agent_backbone.api.models import FileNode

DEFAULT_ALLOWED_PREFIXES = (
    Path("~/orchestration").expanduser().resolve(),
    Path("~/.claude/state").expanduser().resolve(),
    Path("~/.claude/plans").expanduser().resolve(),
    Path("~/notes").expanduser().resolve(),
)


def is_allowed_path(path: Path, *, prefixes: Iterable[Path]) -> bool:
    """Return True when a resolved path sits under an allowed prefix."""
    resolved = path.resolve()
    return any(
        resolved == prefix or str(resolved).startswith(str(prefix) + os.sep) for prefix in prefixes
    )


def build_file_tree(directory: Path, *, depth: int = 2, current_depth: int = 0) -> list[FileNode]:
    """Build a file tree rooted at directory up to a fixed depth."""
    if current_depth >= depth:
        return []
    try:
        entries = sorted(directory.iterdir(), key=lambda path: (not path.is_dir(), path.name))
    except PermissionError:
        return []

    nodes: list[FileNode] = []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            children = (
                build_file_tree(entry, depth=depth, current_depth=current_depth + 1)
                if current_depth + 1 < depth
                else None
            )
            nodes.append(
                FileNode(
                    name=entry.name,
                    type="directory",
                    path=str(entry),
                    children=children,
                )
            )
            continue

        nodes.append(FileNode(name=entry.name, type="file", path=str(entry)))

    return nodes


def read_text_file(path: Path) -> str:
    """Read a UTF-8 text file."""
    return path.read_text()


def write_text_file(path: Path, content: str) -> None:
    """Write text content, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
