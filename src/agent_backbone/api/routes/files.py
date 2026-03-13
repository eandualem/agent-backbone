"""File management endpoints — restricted path browsing, reading, and writing."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from agent_backbone.api.file_access import (
    DEFAULT_ALLOWED_PREFIXES,
    build_file_tree,
    is_allowed_path,
    read_text_file,
    write_text_file,
)
from agent_backbone.api.models import FileNode, FileWriteRequest

router = APIRouter(prefix="/api", tags=["files"])

# Allowed path prefixes (resolved to absolute)
_ALLOWED_PREFIXES = list(DEFAULT_ALLOWED_PREFIXES)


def _is_allowed(path: Path) -> bool:
    """Check if a resolved path is under one of the allowed prefixes."""
    return is_allowed_path(path, prefixes=_ALLOWED_PREFIXES)


def _build_tree(directory: Path, depth: int = 2, current_depth: int = 0) -> list[FileNode]:
    """Build a file tree up to a given depth."""
    return build_file_tree(directory, depth=depth, current_depth=current_depth)


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
        content = read_text_file(target)
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
        write_text_file(target, body.content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write file: {e}") from e
    return {"path": str(target), "written": True}
