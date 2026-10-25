"""Durable content, identity and aggregate admission survive ordinary lifecycle events."""

import asyncio
import sqlite3

import pytest
from sqlalchemy import text

from agent_backbone.models import PublishDocument
from agent_backbone.services.database import (
    BackboneDB,
    DocumentCapacity,
    DocumentConflict,
    DocumentRateLimit,
)
from tests.report_support import author


def publication(name="writer", key="first", **changes):
    return PublishDocument(
        author=name, request_id=key, title="Title", html="<p>body</p>", **changes
    )


async def test_identity_retention_and_backup_restore(tmp_path):
    path = tmp_path / "original.db"
    async with BackboneDB.connect(f"sqlite+aiosqlite:///{path}") as db:
        await author(db)
        first, _ = await db.documents.publish(publication())
        second, _ = await db.documents.publish(
            publication(key="correction", supersedes_id=first["id"])
        )
        await db.agents.rename("writer", "editor")
        retry, created = await db.documents.publish(publication("editor"))
        assert not created and retry["id"] == first["id"] and retry["agent_name"] == "editor"
        assert (await db.documents.query(agent="writer"))["items"] == []
        assert len((await db.documents.query(agent="editor"))["items"]) == 1
        await db.agents.delete("editor")
        await author(db, "editor")
        with pytest.raises(DocumentConflict):
            await db.documents.publish(publication("editor", supersedes_id=second["id"]))
        assert (await db.documents.query(agent="editor"))["items"] == []
        assert (await db.documents.get(first["id"]))["agent_name"] is None
        assert (await db.documents.query(author_id=first["author_id"]))["items"][0]["id"] == second[
            "id"
        ]
        await db.reports.prune(0)
        await db.documents.publish(publication("editor", "new-identity"))
    backup = tmp_path / "restored.db"
    with sqlite3.connect(path) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    async with BackboneDB.connect(f"sqlite+aiosqlite:///{backup}") as db:
        assert (await db.documents.get(first["id"]))["html"] == "<p>body</p>"
        assert (await db.documents.get(first["id"]))["superseded_by"] == second["id"]
        assert (await db.documents.query(author_id=first["author_id"]))["items"][0]["id"] == second[
            "id"
        ]
        assert not (await db.documents.publish(publication("editor", "new-identity")))[1]
        # Retry receipts still exist, with the old author identity, never reassigned.
        async with db.engine.connect() as conn:
            assert (
                await conn.scalar(text("SELECT count(*) FROM documents WHERE request_id='first'"))
                == 1
            )
            assert await conn.scalar(text("SELECT used_bytes FROM document_storage")) > 0


async def test_concurrent_correction_and_global_admission(tmp_path, monkeypatch):
    module = "agent_backbone.services.database._documents_repo."
    async with BackboneDB.connect(f"sqlite+aiosqlite:///{tmp_path}/concurrent.db") as db:
        await author(db)
        await author(db, "other")
        first, _ = await db.documents.publish(publication())
        results = await asyncio.gather(
            *(
                db.documents.publish(publication(key=key, supersedes_id=first["id"]))
                for key in ("a", "b")
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(r, DocumentConflict) for r in results) == 1
        monkeypatch.setattr(module + "DOCUMENTS_PER_HOUR", 3)
        results = await asyncio.gather(
            db.documents.publish(publication(key="global-a")),
            db.documents.publish(publication("other", "global-b")),
            return_exceptions=True,
        )
        assert sum(isinstance(r, DocumentRateLimit) for r in results) == 1
        assert not (await db.documents.publish(publication()))[1]
        monkeypatch.setattr(module + "DOCUMENTS_PER_HOUR", 120)
        monkeypatch.setattr(module + "DOCUMENTS_PER_AUTHOR_HOUR", 1)
        with pytest.raises(DocumentRateLimit):
            await db.documents.publish(publication(key="author-rate"))
        monkeypatch.setattr(module + "DOCUMENTS_PER_AUTHOR_HOUR", 30)
        monkeypatch.setattr(module + "DOCUMENT_STORAGE_BYTES", 0)
        with pytest.raises(DocumentCapacity):
            await db.documents.publish(publication(key="full"))
        assert not (await db.documents.publish(publication()))[1]
        assert (await db.documents.get(first["id"]))["html"] == "<p>body</p>"


async def test_rediscovery_beyond_one_hundred_documents(tmp_path, monkeypatch):
    module = "agent_backbone.services.database._documents_repo."
    monkeypatch.setattr(module + "DOCUMENTS_PER_HOUR", 200)
    monkeypatch.setattr(module + "DOCUMENTS_PER_AUTHOR_HOUR", 200)
    async with BackboneDB.connect(f"sqlite+aiosqlite:///{tmp_path}/paging.db") as db:
        await author(db)
        ids = [(await db.documents.publish(publication(key=str(n))))[0]["id"] for n in range(105)]
        page = await db.documents.query(limit=100)
        assert len(page["items"]) == 100 and page["has_more"]
        # An older document corrected during paging moves above the cursor, never
        # resurfacing as its obsolete version. Refresh discovers that correction.
        corrected, _ = await db.documents.publish(
            publication(key="corrected", supersedes_id=ids[0])
        )
        older = await db.documents.query(before_id=page["next_before_id"], limit=100)
        assert [r["id"] for r in older["items"]] == ids[1:5][::-1]
        assert not older["has_more"]
        assert (await db.documents.query())["items"][0]["id"] == corrected["id"]
