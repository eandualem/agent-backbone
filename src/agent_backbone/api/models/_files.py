"""File-related API models."""

from __future__ import annotations

from pydantic import BaseModel


class FileWriteRequest(BaseModel):
    """Request body for writing file content."""

    path: str
    content: str


class FileNode(BaseModel):
    """File or directory entry."""

    name: str
    type: str  # "file" or "directory"
    path: str
    children: list[FileNode] | None = None


FileNode.model_rebuild()
