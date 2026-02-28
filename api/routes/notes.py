"""Notes CRUD endpoints — file-based notes in ~/notes/."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from api.models import ListEnvelope, NoteDetail, NoteItem

router = APIRouter(prefix="/api", tags=["notes"])

_NOTES_ROOT = Path("~/notes").expanduser()


def _is_safe_path(note_path: Path) -> bool:
    """Ensure resolved path stays under _NOTES_ROOT."""
    resolved = note_path.resolve()
    root_resolved = _NOTES_ROOT.resolve()
    return resolved == root_resolved or str(resolved).startswith(str(root_resolved) + os.sep)


def _note_to_item(note_path: Path) -> NoteItem:
    """Convert a note file path to a NoteItem."""
    rel = note_path.relative_to(_NOTES_ROOT)
    note_id = str(rel)
    try:
        content = note_path.read_text()
        lines = content.splitlines()
        title = lines[0].lstrip("# ").strip() if lines else note_path.stem
        preview = content[:200]
    except OSError:
        title = note_path.stem
        preview = ""
    try:
        mtime = note_path.stat().st_mtime
        modified = datetime.fromtimestamp(mtime, tz=UTC).isoformat()
    except OSError:
        modified = ""
    return NoteItem(id=note_id, title=title, preview=preview, modified=modified)


@router.get("/notes", response_model=ListEnvelope[NoteItem])
async def list_notes(
    subdir: str = Query(default="", description="Subdirectory under ~/notes/"),
):
    """List markdown notes in ~/notes/ (or a subdirectory)."""
    target = _NOTES_ROOT / subdir if subdir else _NOTES_ROOT
    if not _is_safe_path(target):
        raise HTTPException(status_code=403, detail="Path outside notes directory")
    if not target.exists():
        return ListEnvelope(items=[], total=0)

    items: list[NoteItem] = []
    for entry in sorted(target.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        if entry.is_file():
            items.append(_note_to_item(entry))
    return ListEnvelope(items=items, total=len(items))


@router.get("/notes/{note_id:path}", response_model=NoteDetail)
async def get_note(note_id: str):
    """Read full note content by ID (relative path)."""
    note_path = _NOTES_ROOT / note_id
    if not _is_safe_path(note_path):
        raise HTTPException(status_code=403, detail="Path outside notes directory")
    if not note_path.is_file():
        raise HTTPException(status_code=404, detail="Note not found")

    content = note_path.read_text()
    lines = content.splitlines()
    title = lines[0].lstrip("# ").strip() if lines else note_path.stem
    mtime = note_path.stat().st_mtime
    modified = datetime.fromtimestamp(mtime, tz=UTC).isoformat()

    return NoteDetail(id=note_id, title=title, content=content, modified=modified)
