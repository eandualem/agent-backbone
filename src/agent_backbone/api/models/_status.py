"""Status and dashboard API models."""

from __future__ import annotations

from pydantic import BaseModel, Field

from agent_backbone.api.models._agents import EnrichedAgent


class ServiceHealth(BaseModel):
    """Health check for backbone services."""

    gateway: str = "up"
    database: str = "unknown"


class SystemDigest(BaseModel):
    """System-wide status digest."""

    active_sessions: list[str] = Field(default_factory=list)
    agent_count: int = 0
    pending_issues: int | None = 0
    failed_deliveries: int = 0
    agents: list[EnrichedAgent] = Field(default_factory=list)


class DashboardCounts(BaseModel):
    """Aggregate agent counts by state."""

    total: int = 0
    active: int = 0
    idle: int = 0
    busy: int = 0
    plan_waiting: int = 0
    permission_waiting: int = 0
    sub_agent_waiting: int = 0
    starting: int = 0
    offline: int = 0


class DashboardResponse(BaseModel):
    """Unified dashboard payload — everything the dashboard needs in one request."""

    agents: list[EnrichedAgent] = Field(default_factory=list)
    counts: DashboardCounts = Field(default_factory=DashboardCounts)
    plans_pending: int = 0
    issues_pending: int = 0
    failed_deliveries: int = 0
    services: ServiceHealth = Field(default_factory=ServiceHealth)


class RuntimeInfo(BaseModel):
    """Runtime option for agent sessions."""

    id: str
    display_name: str
    available: bool = True
