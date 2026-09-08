"""Operational evidence without terminal output, messages, or log text."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field, field_validator

from agent_backbone.api.deps import get_config, get_db
from agent_backbone.config import BackboneConfig
from agent_backbone.services.database import BackboneDB, diagnostic_details

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])

Severity = Literal["info", "warning", "error"]


class DiagnosticRecord(BaseModel):
    """An explicit metadata projection; extra database fields cannot escape."""

    id: int
    operation_id: str
    category: str
    code: str
    severity: Severity
    agent_name: str = ""
    source: str = ""
    runtime: str = ""
    model: str | None = None
    repo: str = ""
    issue_number: int | None = None
    delivery_id: int | None = None
    queue_id: int | None = None
    event_id: int | None = None
    first_seen_at: str
    last_seen_at: str
    occurrences: int
    details: dict = Field(default_factory=dict)

    @field_validator("details", mode="before")
    @classmethod
    def metadata_only(cls, value):
        return diagnostic_details(value)


class DiagnosticGroup(BaseModel):
    category: str
    code: str
    severity: Severity
    agent_name: str = ""
    runtime: str = ""
    repo: str = ""
    issue_number: int | None = None
    first_seen_at: str
    last_seen_at: str
    occurrences: int
    operation_count: int
    sample_id: int


class DiagnosticCoverage(BaseModel):
    earliest_retained_at: str | None = None
    retention_days: int | None = None
    write_failures_since_process_start: int = 0


class DeliveryHistory(BaseModel):
    attempts: int = 0
    outcomes: dict[str, int] = Field(default_factory=dict)


class QueueMetadata(BaseModel):
    pending: int = 0
    in_progress: int = 0
    oldest_pending_at: str | None = None


class DiagnosticDigest(BaseModel):
    since: str | None
    generated_at: str
    groups: list[DiagnosticGroup]
    total_groups: int
    total_occurrences: int
    has_more: bool
    count_semantics: str
    coverage: DiagnosticCoverage
    informational: dict[str, int] = Field(default_factory=dict)
    deliveries: DeliveryHistory
    queue: QueueMetadata


class DiagnosticRecords(BaseModel):
    items: list[DiagnosticRecord]
    has_more: bool
    next_before_id: int | None = None


class DiagnosticDetail(BaseModel):
    record: DiagnosticRecord
    operation_records: list[DiagnosticRecord]
    has_more: bool


def _since(value: str | None, *, default_window: bool = False) -> str | None:
    if value is None:
        if not default_window:
            return None
        parsed = datetime.now(UTC) - timedelta(hours=24)
    else:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("timezone required")
            return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        except (ValueError, OverflowError) as exc:
            raise HTTPException(422, "since must be an ISO timestamp with a timezone") from exc
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@router.get("", response_model=DiagnosticDigest)
async def diagnostic_digest(
    since: str | None = Query(default=None, max_length=100),
    agent: str | None = Query(default=None, min_length=1, max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
    db: BackboneDB = Depends(get_db),
    config: BackboneConfig = Depends(get_config),
):
    """Group recorded problems; normal delivery waits remain aggregate counts."""
    result = await db.diagnostics.digest(
        since=_since(since, default_window=True), agent_name=agent, limit=limit
    )
    result["coverage"] = {
        **result.get("coverage", {}),
        "retention_days": config.timing.delivery_retention_days,
    }
    return DiagnosticDigest(**result)


@router.get("/records", response_model=DiagnosticRecords)
async def diagnostic_records(
    since: str | None = Query(default=None, max_length=100),
    agent: str | None = Query(default=None, min_length=1, max_length=200),
    category: str | None = Query(default=None, min_length=1, max_length=100),
    severity: Severity | None = Query(default=None),
    operation_id: str | None = Query(default=None, min_length=1, max_length=200),
    limit: int = Query(default=100, ge=1, le=100),
    before_id: int | None = Query(default=None, ge=1),
    db: BackboneDB = Depends(get_db),
):
    """Page through explicit diagnostic metadata, newest record IDs first."""
    rows = await db.diagnostics.query(
        since=_since(since),
        agent_name=agent,
        category=category,
        severity=severity,
        operation_id=operation_id,
        limit=limit + 1,
        before_id=before_id,
    )
    has_more = len(rows) > limit
    items = [DiagnosticRecord(**row) for row in rows[:limit]]
    return DiagnosticRecords(
        items=items,
        has_more=has_more,
        next_before_id=items[-1].id if has_more and items else None,
    )


@router.get("/{diagnostic_id}", response_model=DiagnosticDetail)
async def diagnostic_detail(
    diagnostic_id: int = Path(ge=1),
    db: BackboneDB = Depends(get_db),
):
    """One record and at most 100 records with its exact operation identity."""
    row = await db.diagnostics.get(diagnostic_id)
    if row is None:
        raise HTTPException(404, "Diagnostic record not found; it may have been pruned")
    records = await db.diagnostics.query(operation_id=row["operation_id"], limit=101)
    return DiagnosticDetail(
        record=DiagnosticRecord(**row),
        operation_records=[DiagnosticRecord(**item) for item in records[:100]],
        has_more=len(records) > 100,
    )
