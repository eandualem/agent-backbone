"""Hierarchy endpoint — canonical organizational tree with live state."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends

from agent_backbone.api.deps import get_config, get_db, get_state_service, get_tmux_service
from agent_backbone.api.models import (
    CodingAgentNode,
    HierarchyNode,
    HierarchyResponse,
    HierarchySection,
    HierarchyState,
    HierarchySwarmWorkerNode,
    HierarchyTier,
)
from agent_backbone.config import BackboneConfig
from agent_backbone.services.agents import AgentState, StateService
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.terminal import TmuxService

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["hierarchy"])

_STATE_PRIORITY: tuple[HierarchyState, ...] = (
    "busy",
    "processing",
    "starting",
    "plan_waiting",
    "idle",
    "offline",
)
_TERMINAL_SWARM_PHASES = frozenset({"merged", "cleaned_up", "failed", "discarded"})


@dataclass(frozen=True)
class _HierarchyNodeSpec:
    """Static backend-owned hierarchy node definition."""

    id: str
    label: str
    role: str
    tier: HierarchyTier
    session: str | None
    managed_org: str | None = None
    children: tuple[_HierarchyNodeSpec, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class _HierarchySectionSpec:
    """Static backend-owned section definition."""

    id: str
    title: str
    nodes: tuple[_HierarchyNodeSpec, ...]


_ROOT_NODE = _HierarchyNodeSpec(
    id="jarvis",
    label="Jarvis",
    role="Personal Assistant",
    tier="assistant",
    session=None,
)

_STRATEGY_NODE = _HierarchyNodeSpec(
    id="leo",
    label="Da Vinci",
    role="Strategy Co-Architect",
    tier="strategic",
    session="leo",
)

_INDEPENDENT_PEERS = (
    _HierarchyNodeSpec(
        id="feynman",
        label="Feynman",
        role="Orchestration Optimizer",
        tier="independent-peer",
        session="feynman",
    ),
    _HierarchyNodeSpec(
        id="brunel",
        label="Brunel",
        role="Infrastructure Agent",
        tier="independent-peer",
        session="brunel",
    ),
)

_SECTIONS = (
    _HierarchySectionSpec(
        id="operations",
        title="Operations",
        nodes=(
            _HierarchyNodeSpec(
                id="ike",
                label="Eisenhower",
                role="Core Orchestrator",
                tier="orchestrator",
                session="ike",
                children=(
                    _HierarchyNodeSpec(
                        id="bell-wf",
                        label="Bell (WF)",
                        role="Org Orchestrator",
                        tier="sub-orchestrator",
                        session="bell-wf",
                        managed_org="WF",
                    ),
                    _HierarchyNodeSpec(
                        id="bell-loveble",
                        label="Bell (Loveble)",
                        role="Org Orchestrator",
                        tier="sub-orchestrator",
                        session="bell-loveble",
                        managed_org="Loveble",
                    ),
                ),
            ),
            _HierarchyNodeSpec(
                id="ada",
                label="Lovelace",
                role="Spec Methodology Architect",
                tier="orchestrator",
                session="ada",
                children=(
                    _HierarchyNodeSpec(
                        id="euclid-wf",
                        label="Euclid (WF)",
                        role="Spec Manager",
                        tier="sub-orchestrator",
                        session="euclid-wf",
                    ),
                    _HierarchyNodeSpec(
                        id="euclid-loveble",
                        label="Euclid (Loveble)",
                        role="Spec Manager",
                        tier="sub-orchestrator",
                        session="euclid-loveble",
                    ),
                ),
            ),
        ),
    ),
    _HierarchySectionSpec(
        id="review",
        title="Review",
        nodes=(
            _HierarchyNodeSpec(
                id="oppenheimer",
                label="Oppenheimer",
                role="Review Process Architect",
                tier="reviewer",
                session="oppenheimer",
                children=(
                    _HierarchyNodeSpec(
                        id="hemingway-wf",
                        label="Hemingway (WF)",
                        role="Code Reviewer",
                        tier="reviewer",
                        session="hemingway-wf",
                        managed_org="WF",
                    ),
                    _HierarchyNodeSpec(
                        id="hemingway-loveble",
                        label="Hemingway (Loveble)",
                        role="Code Reviewer",
                        tier="reviewer",
                        session="hemingway-loveble",
                        managed_org="Loveble",
                    ),
                ),
            ),
        ),
    ),
    _HierarchySectionSpec(
        id="architecture",
        title="Architecture",
        nodes=(
            _HierarchyNodeSpec(
                id="wright",
                label="Wright",
                role="System Architect",
                tier="architect",
                session="wright",
                children=(
                    _HierarchyNodeSpec(
                        id="gaudi-wf",
                        label="Gaudi (WF)",
                        role="Repo Architect",
                        tier="architect",
                        session="gaudi-wf",
                        managed_org="WF",
                    ),
                    _HierarchyNodeSpec(
                        id="gaudi-loveble",
                        label="Gaudi (Loveble)",
                        role="Repo Architect",
                        tier="architect",
                        session="gaudi-loveble",
                        managed_org="Loveble",
                    ),
                ),
            ),
        ),
    ),
    _HierarchySectionSpec(
        id="knowledge-workers",
        title="Knowledge Workers",
        nodes=(
            _HierarchyNodeSpec(
                id="darwin",
                label="Darwin",
                role="System Evolution Architect",
                tier="knowledge-worker",
                session="darwin",
            ),
            _HierarchyNodeSpec(
                id="gallup",
                label="Gallup",
                role="Market Research",
                tier="knowledge-worker",
                session="gallup",
            ),
            _HierarchyNodeSpec(
                id="twain",
                label="Twain",
                role="Social Content Agent",
                tier="knowledge-worker",
                session="twain",
            ),
            _HierarchyNodeSpec(
                id="durant",
                label="Durant",
                role="Narrative Historian",
                tier="knowledge-worker",
                session="durant",
            ),
        ),
    ),
)


def _iter_specs() -> list[_HierarchyNodeSpec]:
    """Flatten the static hierarchy into a pre-order list."""
    flattened: list[_HierarchyNodeSpec] = [_ROOT_NODE, _STRATEGY_NODE, *_INDEPENDENT_PEERS]

    def visit(node: _HierarchyNodeSpec) -> None:
        flattened.append(node)
        for child in node.children:
            visit(child)

    for section in _SECTIONS:
        for node in section.nodes:
            visit(node)

    return flattened


def _managed_orgs() -> set[str]:
    """Managed organizations owned by sub-orchestrator nodes."""
    return {node.managed_org for node in _iter_specs() if node.managed_org is not None}


def _normalize_state(raw_state: AgentState, online: bool) -> HierarchyState:
    """Map backbone agent state to hierarchy display state."""
    if not online:
        return "offline"

    if raw_state == AgentState.PROCESSING_ISSUE:
        return "processing"
    if raw_state == AgentState.PERMISSION_WAITING:
        return "plan_waiting"
    if raw_state == AgentState.UNKNOWN:
        return "idle"
    return raw_state.value  # type: ignore[return-value]


def _summarize_states(states: list[HierarchyState]) -> HierarchyState:
    """Highest-priority state across direct entity matches."""
    if not states:
        return "offline"

    for state in _STATE_PRIORITY:
        if state in states:
            return state
    return "offline"


async def _load_snapshot_details(
    state_svc: StateService,
    sessions: set[str],
) -> dict[str, object]:
    """Fetch state snapshots keyed by session."""
    ordered_sessions = sorted(sessions)
    snapshots = await asyncio.gather(
        *(state_svc.get_state(session) for session in ordered_sessions)
    )
    return {
        session: snapshot for session, snapshot in zip(ordered_sessions, snapshots, strict=False)
    }


async def _load_active_swarm_workers(
    db: BackboneDB,
    active_sessions: set[str],
) -> dict[str, list[HierarchySwarmWorkerNode]]:
    """Group active swarm workers by parent coding-agent session.

    Filters out swarms in terminal phases AND swarms where no worker
    session is alive (catches cleaned-up swarms stuck in non-terminal phase).
    """
    try:
        swarms = await db.list_swarms()
    except Exception:
        log.exception("Failed to list swarms for hierarchy")
        return {}

    active_swarms = [swarm for swarm in swarms if swarm.get("phase") not in _TERMINAL_SWARM_PHASES]
    if not active_swarms:
        return {}

    details = await asyncio.gather(
        *(db.get_swarm(swarm["swarm_id"]) for swarm in active_swarms),
        return_exceptions=True,
    )
    grouped: dict[str, list[HierarchySwarmWorkerNode]] = defaultdict(list)

    for detail in details:
        if isinstance(detail, Exception):
            log.exception("Failed to load swarm detail for hierarchy", exc_info=detail)
            continue
        if not detail:
            continue

        session_name = detail.get("coding_agent_session")
        if not isinstance(session_name, str):
            continue

        workers = detail.get("workers", [])

        # Skip swarms with no live worker sessions (#15)
        if not any(w["session"] in active_sessions for w in workers):
            continue

        for worker in workers:
            grouped[session_name].append(
                HierarchySwarmWorkerNode(
                    id=worker.get("worker_id") or worker["name"],
                    name=worker["name"],
                    role=worker.get("role", "unknown"),
                    session=worker["session"],
                    branch=worker["branch"],
                    status=worker.get("status", "pending"),
                )
            )

    for workers in grouped.values():
        workers.sort(key=lambda worker: worker.name.casefold())

    return dict(grouped)


def _resolve_role(config: BackboneConfig, spec: _HierarchyNodeSpec) -> str:
    """Prefer registry role data when the entity exists."""
    if spec.session is not None:
        reg_entry = config.registry.entry_for_session(spec.session)
        if reg_entry and reg_entry.role:
            return reg_entry.role
    reg_entry = config.registry.entities.get(spec.id)
    if reg_entry and reg_entry.role:
        return reg_entry.role
    return spec.role


def _build_coding_agent_nodes(
    config: BackboneConfig,
    active_sessions: set[str],
    snapshots: dict[str, object],
    swarm_workers_by_agent: dict[str, list[HierarchySwarmWorkerNode]],
) -> list[CodingAgentNode]:
    """Build coding-agent nodes from discovered repos."""
    nodes: list[CodingAgentNode] = []
    for repo in sorted(
        config.registry.repos,
        key=lambda item: (item.org.casefold(), item.name.casefold()),
    ):
        snapshot = snapshots.get(repo.name)
        online = repo.name in active_sessions
        state = _normalize_state(snapshot.state, online) if snapshot else "offline"
        nodes.append(
            CodingAgentNode(
                id=repo.name,
                label=repo.name,
                org=repo.org,
                state=state,
                online=online,
                current_issue=snapshot.current_issue if snapshot else None,
                swarm_workers=swarm_workers_by_agent.get(repo.name) or None,
            )
        )
    return nodes


def _build_named_node(
    spec: _HierarchyNodeSpec,
    config: BackboneConfig,
    active_sessions: set[str],
    snapshots: dict[str, object],
    coding_agents_by_org: dict[str, list[CodingAgentNode]],
) -> HierarchyNode:
    """Build one named hierarchy node recursively."""
    direct_states: list[HierarchyState] = []
    if spec.session is not None:
        snapshot = snapshots.get(spec.session)
        if snapshot is not None:
            direct_states.append(_normalize_state(snapshot.state, spec.session in active_sessions))

    children = [
        _build_named_node(child, config, active_sessions, snapshots, coding_agents_by_org)
        for child in spec.children
    ]
    coding_agents = None
    if spec.managed_org is not None:
        coding_agents = list(coding_agents_by_org.get(spec.managed_org, []))

    return HierarchyNode(
        id=spec.id,
        label=spec.label,
        role=_resolve_role(config, spec),
        tier=spec.tier,
        state=_summarize_states(direct_states),
        session=spec.session,
        online=spec.session in active_sessions if spec.session is not None else False,
        managed_org=spec.managed_org,
        children=children or None,
        coding_agents=coding_agents,
    )


def _coding_agents_by_org(coding_agents: list[CodingAgentNode]) -> dict[str, list[CodingAgentNode]]:
    """Group coding agents by org and sort them by label."""
    grouped: dict[str, list[CodingAgentNode]] = defaultdict(list)
    for agent in coding_agents:
        grouped[agent.org].append(agent)

    for agents in grouped.values():
        agents.sort(key=lambda agent: agent.label.casefold())

    return dict(grouped)


@router.get("/hierarchy", response_model=HierarchyResponse)
async def get_hierarchy(
    config: BackboneConfig = Depends(get_config),
    db: BackboneDB = Depends(get_db),
    state_svc: StateService = Depends(get_state_service),
    tmux_svc: TmuxService = Depends(get_tmux_service),
):
    """Return the canonical organizational hierarchy with live state."""
    static_sessions = {spec.session for spec in _iter_specs() if spec.session is not None}
    repo_sessions = {repo.name for repo in config.registry.repos}
    sessions = static_sessions | repo_sessions

    active_sessions = set(await tmux_svc.list_sessions())
    snapshots = await _load_snapshot_details(state_svc, sessions)
    swarm_workers_by_agent = await _load_active_swarm_workers(db, active_sessions)

    coding_agents = _build_coding_agent_nodes(
        config,
        active_sessions,
        snapshots,
        swarm_workers_by_agent,
    )
    coding_agents_by_org = _coding_agents_by_org(coding_agents)
    assigned_orgs = _managed_orgs()

    sections = [
        HierarchySection(
            id=section.id,
            title=section.title,
            nodes=[
                _build_named_node(node, config, active_sessions, snapshots, coding_agents_by_org)
                for node in section.nodes
            ],
        )
        for section in _SECTIONS
    ]

    return HierarchyResponse(
        root=_build_named_node(
            _ROOT_NODE,
            config,
            active_sessions,
            snapshots,
            coding_agents_by_org,
        ),
        strategy=_build_named_node(
            _STRATEGY_NODE,
            config,
            active_sessions,
            snapshots,
            coding_agents_by_org,
        ),
        independent_peers=[
            _build_named_node(node, config, active_sessions, snapshots, coding_agents_by_org)
            for node in _INDEPENDENT_PEERS
        ],
        sections=sections,
        unassigned_coding_agents=sorted(
            (agent for agent in coding_agents if agent.org not in assigned_orgs),
            key=lambda agent: (agent.org.casefold(), agent.label.casefold()),
        ),
    )
