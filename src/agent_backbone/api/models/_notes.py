"""Note-related API models."""

from __future__ import annotations

from pydantic import BaseModel


class NoteCreate(BaseModel):
    """Request body for creating a note."""

    title: str
    content: str
    subdir: str = ""


class NoteUpdate(BaseModel):
    """Request body for updating a note."""

    content: str


class NoteItem(BaseModel):
    """Note list entry (preview)."""

    id: str  # relative path from ~/notes/
    title: str
    preview: str = ""
    modified: str = ""  # ISO 8601


class NoteDetail(BaseModel):
    """Full note content."""

    id: str
    title: str
    content: str
    modified: str = ""
