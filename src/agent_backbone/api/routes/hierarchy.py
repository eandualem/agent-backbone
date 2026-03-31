"""Hierarchy endpoint — canonical organizational tree with live state."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from fastapi import APIRouter, Depends

from agent_backbone.api.deps import get_config, get_db, get_state_service, get_tmux_service
from agent_backbone.api.models import (
    CodingAgentNode,
    HierarchyNode,
    HierarchyResponse,
    HierarchySection,
    HierarchyState,
    HierarchySwarmWorkerNode,
)
from agent_backbone.config import BackboneConfig
from agent_backbone.services.agents import AgentState, StateService
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.registry.models import EntityEntry
from agent_backbone.services.terminal import TmuxService

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["hierarchy"])

_STATE_PRIORITY: tuple[HierarchyState, ...] = (
    "busy",
    "permission_waiting",
    "plan_waiting",
    "sub_agent_waiting",
    "starting",
    "idle",
    "offline",
)
_TERMINAL_SWARM_PHASES = frozenset({"merged", "cleaned_up", "failed", "discarded"})


def _normalize_state(raw_state: AgentState, online: bool) -> HierarchyState:
    """Map backbone agent state to hierarchy display state."""
    if not online:
        return "offline"

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


def _build_node_from_entry(
    entity_id: str,
    entry: EntityEntry,
    config: BackboneConfig,
    active_sessions: set[str],
    snapshots: dict[str, object],
    coding_agents_by_org: dict[str, list[CodingAgentNode]],
) -> HierarchyNode:
    """Build one named hierarchy node from a registry entry, recursing into children."""
    direct_states: list[HierarchyState] = []
    if entry.session is not None:
        snapshot = snapshots.get(entry.session)
        if snapshot is not None:
            direct_states.append(_normalize_state(snapshot.state, entry.session in active_sessions))

    children = [
        _build_node_from_entry(
            child_id, child_entry, config, active_sessions, snapshots, coding_agents_by_org,
        )
        for child_id, child_entry in config.registry.get_children(entity_id)
    ]

    coding_agents = None
    if entry.managed_org is not None:
        coding_agents = list(coding_agents_by_org.get(entry.managed_org, []))

    return HierarchyNode(
        id=entity_id,
        label=entry.label or entity_id,
        role=entry.role,
        tier=entry.tier or "agent",
        state=_summarize_states(direct_states),
        session=entry.session,
        online=entry.session in active_sessions if entry.session is not None else False,
        managed_org=entry.managed_org,
        children=children or None,
        coding_agents=coding_agents,
    )


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
    # Collect all sessions: entity sessions + repo sessions
    entity_sessions = {
        entry.session
        for entry in config.registry.entities.values()
        if entry.session is not None
    }
    repo_sessions = {repo.name for repo in config.registry.repos}
    sessions = entity_sessions | repo_sessions

    active_sessions = set(await tmux_svc.list_sessions())
    snapshots = await _load_snapshot_details(state_svc, sessions)
    swarm_workers_by_agent = await _load_active_swarm_workers(db, active_sessions)

    coding_agents = _build_coding_agent_nodes(
        config,
        active_sessions,
        snapshots,
        swarm_workers_by_agent,
    )
    ca_by_org = _coding_agents_by_org(coding_agents)

    # Build root (assistant tier)
    assistant_entries = config.registry.get_entities_by_tier("assistant")
    root_id, root_entry = assistant_entries[0]
    root = _build_node_from_entry(
        root_id, root_entry, config, active_sessions, snapshots, ca_by_org,
    )

    # Build strategy node
    strategic_entries = config.registry.get_entities_by_tier("strategic")
    strat_id, strat_entry = strategic_entries[0]
    strategy = _build_node_from_entry(
        strat_id, strat_entry, config, active_sessions, snapshots, ca_by_org,
    )

    # Build independent peers
    independent_peers = [
        _build_node_from_entry(eid, entry, config, active_sessions, snapshots, ca_by_org)
        for eid, entry in config.registry.get_entities_by_tier("independent-peer")
    ]

    # Build sections from registry
    section_list = []
    for section_id, title, _order in config.registry.get_sections():
        section_nodes = [
            _build_node_from_entry(eid, entry, config, active_sessions, snapshots, ca_by_org)
            for eid, entry in config.registry.get_entities_in_section(section_id)
        ]
        section_list.append(HierarchySection(id=section_id, title=title, nodes=section_nodes))

    # Managed orgs: scan all entities for managed_org
    managed_orgs = {
        entry.managed_org
        for entry in config.registry.entities.values()
        if entry.managed_org is not None
    }

    return HierarchyResponse(
        root=root,
        strategy=strategy,
        independent_peers=independent_peers,
        sections=section_list,
        unassigned_coding_agents=sorted(
            (agent for agent in coding_agents if agent.org not in managed_orgs),
            key=lambda agent: (agent.org.casefold(), agent.label.casefold()),
        ),
    )
