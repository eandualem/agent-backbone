"""Delivery-related API models."""

from __future__ import annotations

from pydantic import BaseModel


class DeliveryRecord(BaseModel):
    """A single delivery attempt record."""

    id: int
    repo_full_name: str = ""
    issue_number: int
    target_entity: str
    session_name: str
    outcome: str
    flow_name: str = ""
    created_at: str = ""


class DeliveryStats(BaseModel):
    """Aggregated delivery statistics."""

    total: int = 0
    delivered: int = 0
    failed: int = 0
    deferred: int = 0
    offline: int = 0
