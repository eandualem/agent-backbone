"""Notes CRUD endpoints — file-based notes in ~/notes/."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from agent_backbone.api.models import ListEnvelope, NoteCreate, NoteDetail, NoteItem, NoteUpdate

router = APIRouter(prefix="/api", tags=["notes"])

_NOTES_ROOT = Path("~/notes").expanduser()


class _NoteRouteError(Exception):
    """Internal error used to surface sync helper failures to async handlers."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


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


def _list_notes_sync(target: Path) -> list[NoteItem]:
    """Sync helper for listing notes under a target directory."""
    if not _is_safe_path(target):
        raise _NoteRouteError(status_code=403, detail="Path outside notes directory")
    if not target.exists():
        return []

    items: list[NoteItem] = []
    for entry in sorted(target.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        if entry.is_file():
            items.append(_note_to_item(entry))
    return items


def _read_note_sync(note_path: Path, note_id: str) -> NoteDetail:
    """Sync helper for reading one note."""
    if not _is_safe_path(note_path):
        raise _NoteRouteError(status_code=403, detail="Path outside notes directory")
    if not note_path.is_file():
        raise _NoteRouteError(status_code=404, detail="Note not found")

    content = note_path.read_text()
    lines = content.splitlines()
    title = lines[0].lstrip("# ").strip() if lines else note_path.stem
    mtime = note_path.stat().st_mtime
    modified = datetime.fromtimestamp(mtime, tz=UTC).isoformat()
    return NoteDetail(id=note_id, title=title, content=content, modified=modified)


def _create_note_sync(note_path: Path, content: str, title: str) -> NoteDetail:
    """Sync helper for creating one note."""
    if not _is_safe_path(note_path):
        raise _NoteRouteError(status_code=403, detail="Path outside notes directory")

    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(content)

    note_id = str(note_path.relative_to(_NOTES_ROOT))
    lines = content.splitlines()
    resolved_title = lines[0].lstrip("# ").strip() if lines else title
    mtime = note_path.stat().st_mtime
    modified = datetime.fromtimestamp(mtime, tz=UTC).isoformat()

    return NoteDetail(id=note_id, title=resolved_title, content=content, modified=modified)


def _update_note_sync(note_path: Path, note_id: str, content: str) -> NoteDetail:
    """Sync helper for updating one note."""
    if not _is_safe_path(note_path):
        raise _NoteRouteError(status_code=403, detail="Path outside notes directory")
    if not note_path.is_file():
        raise _NoteRouteError(status_code=404, detail="Note not found")

    note_path.write_text(content)

    lines = content.splitlines()
    title = lines[0].lstrip("# ").strip() if lines else note_path.stem
    mtime = note_path.stat().st_mtime
    modified = datetime.fromtimestamp(mtime, tz=UTC).isoformat()

    return NoteDetail(id=note_id, title=title, content=content, modified=modified)


def _delete_note_sync(note_path: Path, note_id: str) -> dict[str, object]:
    """Sync helper for deleting one note."""
    if not _is_safe_path(note_path):
        raise _NoteRouteError(status_code=403, detail="Path outside notes directory")
    if not note_path.is_file():
        raise _NoteRouteError(status_code=404, detail="Note not found")

    note_path.unlink()
    return {"deleted": True, "id": note_id}


@router.get("/notes", response_model=ListEnvelope[NoteItem])
async def list_notes(
    subdir: str = Query(default="", description="Subdirectory under ~/notes/"),
):
    """List markdown notes in ~/notes/ (or a subdirectory)."""
    target = _NOTES_ROOT / subdir if subdir else _NOTES_ROOT
    try:
        items = await asyncio.to_thread(_list_notes_sync, target)
    except _NoteRouteError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return ListEnvelope(items=items, total=len(items))


@router.get("/notes/{note_id:path}", response_model=NoteDetail)
async def get_note(note_id: str):
    """Read full note content by ID (relative path)."""
    note_path = _NOTES_ROOT / note_id
    try:
        return await asyncio.to_thread(_read_note_sync, note_path, note_id)
    except _NoteRouteError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post("/notes", response_model=NoteDetail, status_code=201)
async def create_note(body: NoteCreate):
    """Create a new markdown note in ~/notes/."""
    target_dir = _NOTES_ROOT / body.subdir if body.subdir else _NOTES_ROOT

    # Sanitize title into filename
    safe_name = "".join(c if c.isalnum() or c in "-_ " else "" for c in body.title).strip()
    if not safe_name:
        raise HTTPException(status_code=400, detail="Title must contain valid filename characters")
    filename = safe_name.replace(" ", "-").lower() + ".md"
    note_path = target_dir / filename

    try:
        return await asyncio.to_thread(_create_note_sync, note_path, body.content, body.title)
    except _NoteRouteError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.put("/notes/{note_id:path}", response_model=NoteDetail)
async def update_note(note_id: str, body: NoteUpdate):
    """Update an existing note's content."""
    note_path = _NOTES_ROOT / note_id
    try:
        return await asyncio.to_thread(_update_note_sync, note_path, note_id, body.content)
    except _NoteRouteError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.delete("/notes/{note_id:path}")
async def delete_note(note_id: str):
    """Delete a note by ID."""
    note_path = _NOTES_ROOT / note_id
    try:
        return await asyncio.to_thread(_delete_note_sync, note_path, note_id)
    except _NoteRouteError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
