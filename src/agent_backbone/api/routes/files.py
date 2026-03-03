"""File management endpoints — restricted path browsing, reading, and writing."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from agent_backbone.api.models import FileNode, FileWriteRequest

router = APIRouter(prefix="/api", tags=["files"])

# Allowed path prefixes (resolved to absolute)
_ALLOWED_PREFIXES = [
    Path("~/orchestration").expanduser().resolve(),
    Path("~/.claude/state").expanduser().resolve(),
    Path("~/.claude/plans").expanduser().resolve(),
    Path("~/notes").expanduser().resolve(),
]


def _is_allowed(path: Path) -> bool:
    """Check if a resolved path is under one of the allowed prefixes."""
    resolved = path.resolve()
    return any(
        resolved == prefix or str(resolved).startswith(str(prefix) + os.sep)
        for prefix in _ALLOWED_PREFIXES
    )


def _build_tree(directory: Path, depth: int = 2, current_depth: int = 0) -> list[FileNode]:
    """Build a file tree up to a given depth."""
    if current_depth >= depth:
        return []
    try:
        entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name))
    except PermissionError:
        return []

    nodes: list[FileNode] = []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            children = (
                _build_tree(entry, depth, current_depth + 1) if current_depth + 1 < depth else None
            )
            nodes.append(
                FileNode(
                    name=entry.name,
                    type="directory",
                    path=str(entry),
                    children=children,
                )
            )
        else:
            nodes.append(FileNode(name=entry.name, type="file", path=str(entry)))
    return nodes


@router.get("/files/tree", response_model=list[FileNode])
async def get_file_tree(
    path: str = Query(..., description="Root directory to list"),
    depth: int = Query(default=2, ge=1, le=5),
):
    """Browse a directory tree. Restricted to allowed paths."""
    target = Path(path).expanduser()
    if not _is_allowed(target):
        raise HTTPException(status_code=403, detail="Path not in allowed directories")
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="Directory not found")
    return _build_tree(target, depth=depth)


@router.get("/files/content")
async def get_file_content(
    path: str = Query(..., description="File path to read"),
):
    """Read file contents. Restricted to allowed paths."""
    target = Path(path).expanduser()
    if not _is_allowed(target):
        raise HTTPException(status_code=403, detail="Path not in allowed directories")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    try:
        content = target.read_text()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read file: {e}") from e
    return {"path": str(target), "content": content}


@router.post("/files/content")
async def write_file_content(body: FileWriteRequest):
    """Write content to a file. Restricted to allowed paths."""
    target = Path(body.path).expanduser()
    if not _is_allowed(target):
        raise HTTPException(status_code=403, detail="Path not in allowed directories")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body.content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write file: {e}") from e
    return {"path": str(target), "written": True}
