"""Hierarchy-related API models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

HierarchyTier = Literal[
    "root",
    "assistant",
    "strategic",
    "orchestrator",
    "sub-orchestrator",
    "independent-peer",
    "knowledge-worker",
    "reviewer",
    "architect",
    "quality",
    "coding-agent",
    "swarm-worker",
]

HierarchyState = Literal[
    "idle",
    "busy",
    "plan_waiting",
    "permission_waiting",
    "sub_agent_waiting",
    "offline",
    "starting",
]


class HierarchySwarmWorkerNode(BaseModel):
    """Swarm worker attached under a coding agent."""

    id: str
    name: str
    role: str
    session: str
    branch: str
    status: str


class CodingAgentNode(BaseModel):
    """Dynamic coding agent attached to a hierarchy node."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    label: str
    org: str
    state: HierarchyState
    online: bool
    current_issue: int | None = Field(
        default=None,
        alias="currentIssue",
        serialization_alias="currentIssue",
    )
    swarm_workers: list[HierarchySwarmWorkerNode] | None = Field(
        default=None,
        alias="swarmWorkers",
        serialization_alias="swarmWorkers",
    )


class HierarchyNode(BaseModel):
    """Static named node in the organizational hierarchy."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    label: str
    role: str
    tier: HierarchyTier
    state: HierarchyState
    session: str | None
    online: bool
    managed_org: str | None = Field(
        default=None,
        alias="managedOrg",
        serialization_alias="managedOrg",
    )
    children: list[HierarchyNode] | None = None
    coding_agents: list[CodingAgentNode] | None = Field(
        default=None,
        alias="codingAgents",
        serialization_alias="codingAgents",
    )


class HierarchySection(BaseModel):
    """Display section for rendering named hierarchy groups."""

    id: str
    title: str
    nodes: list[HierarchyNode] = Field(default_factory=list)


class HierarchyResponse(BaseModel):
    """Full hierarchy response for the org-chart API."""

    model_config = ConfigDict(populate_by_name=True)

    root: HierarchyNode
    strategy: HierarchyNode
    independent_peers: list[HierarchyNode] = Field(
        default_factory=list,
        alias="independentPeers",
        serialization_alias="independentPeers",
    )
    sections: list[HierarchySection] = Field(default_factory=list)
    unassigned_coding_agents: list[CodingAgentNode] = Field(
        default_factory=list,
        alias="unassignedCodingAgents",
        serialization_alias="unassignedCodingAgents",
    )


HierarchyNode.model_rebuild()
