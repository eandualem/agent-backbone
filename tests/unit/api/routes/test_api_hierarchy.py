"""Tests for api/routes/hierarchy.py — canonical hierarchy endpoint."""

from __future__ import annotations

import time
from dataclasses import replace as dc_replace
from unittest.mock import AsyncMock, MagicMock

from agent_backbone.api.deps import get_state_service, get_tmux_service
from agent_backbone.services.agents import AgentState, StateSnapshot
from agent_backbone.services.registry import EntityEntry, EntityRegistry, RepoInfo


def _snapshot(
    state: AgentState = AgentState.IDLE,
    *,
    issue: int | None = None,
) -> StateSnapshot:
    """Build a state snapshot for hierarchy tests."""
    return StateSnapshot(
        state=state,
        current_issue=issue,
        source="push",
        timestamp=time.time(),
    )


def _hierarchy_registry() -> EntityRegistry:
    """Registry matching the canonical hierarchy tree."""
    return EntityRegistry(
        entities={
            "jarvis": EntityEntry(
                session=None,
                home="~/ws/jarvis",
                groups=[],
                figure="Jarvis",
                role="Personal Assistant",
                entity_type="service",
                label="Jarvis",
                tier="assistant",
            ),
            "leo": EntityEntry(
                session="leo",
                home="~/ws/core/leo",
                groups=["orchestrators"],
                figure="Leonardo da Vinci",
                role="Strategy Co-Architect",
                label="Da Vinci",
                tier="strategic",
            ),
            "feynman": EntityEntry(
                session="feynman",
                home="~/ws/core/feynman",
                groups=["orchestrators"],
                figure="Richard Feynman",
                role="Orchestration Optimizer",
                label="Feynman",
                tier="independent-peer",
            ),
            "brunel": EntityEntry(
                session="brunel",
                home="~/ws/core/brunel",
                groups=["orchestrators"],
                figure="Isambard Kingdom Brunel",
                role="Infrastructure Agent",
                label="Brunel",
                tier="independent-peer",
            ),
            "ike": EntityEntry(
                session="ike",
                home="~/ws/core/ike",
                groups=["orchestrators"],
                figure="Dwight Eisenhower",
                role="Core Orchestrator",
                label="Eisenhower",
                tier="orchestrator",
                section="operations",
                section_order=1,
            ),
            "bell-wf": EntityEntry(
                session="bell-wf",
                home="~/ws/core/bell-wf",
                groups=["orchestrators"],
                figure="Alexander Graham Bell",
                role="Org Orchestrator",
                organization="WF",
                entity_type="role-instance",
                label="Bell (WF)",
                tier="sub-orchestrator",
                section="operations",
                section_order=1,
                parent="ike",
                managed_org="WF",
            ),
            "bell-loveble": EntityEntry(
                session="bell-loveble",
                home="~/ws/core/bell-loveble",
                groups=["orchestrators"],
                figure="Alexander Graham Bell",
                role="Org Orchestrator",
                organization="Loveble",
                entity_type="role-instance",
                label="Bell (Loveble)",
                tier="sub-orchestrator",
                section="operations",
                section_order=1,
                parent="ike",
                managed_org="Loveble",
            ),
            "ada": EntityEntry(
                session="ada",
                home="~/ws/core/ada",
                groups=["orchestrators"],
                figure="Ada Lovelace",
                role="Spec Methodology Architect",
                label="Lovelace",
                tier="orchestrator",
                section="operations",
                section_order=1,
            ),
            "euclid-wf": EntityEntry(
                session="euclid-wf",
                home="~/ws/core/euclid-wf",
                groups=["orchestrators"],
                figure="Euclid",
                role="Spec Manager",
                organization="WF",
                entity_type="role-instance",
                label="Euclid (WF)",
                tier="sub-orchestrator",
                section="operations",
                section_order=1,
                parent="ada",
            ),
            "euclid-loveble": EntityEntry(
                session="euclid-loveble",
                home="~/ws/core/euclid-loveble",
                groups=["orchestrators"],
                figure="Euclid",
                role="Spec Manager",
                organization="Loveble",
                entity_type="role-instance",
                label="Euclid (Loveble)",
                tier="sub-orchestrator",
                section="operations",
                section_order=1,
                parent="ada",
            ),
            "oppenheimer": EntityEntry(
                session="oppenheimer",
                home="~/ws/core/review/oppenheimer",
                groups=["reviewers"],
                figure="J. Robert Oppenheimer",
                role="Review Process Architect",
                label="Oppenheimer",
                tier="reviewer",
                section="review",
                section_order=2,
            ),
            "hemingway-wf": EntityEntry(
                session="hemingway-wf",
                home="~/ws/core/review/WF/hemingway",
                groups=["reviewers"],
                figure="Ernest Hemingway",
                role="Code Reviewer",
                organization="WF",
                entity_type="role-instance",
                label="Hemingway (WF)",
                tier="reviewer",
                section="review",
                section_order=2,
                parent="oppenheimer",
                managed_org="WF",
            ),
            "hemingway-loveble": EntityEntry(
                session="hemingway-loveble",
                home="~/ws/core/review/Loveble/hemingway",
                groups=["reviewers"],
                figure="Ernest Hemingway",
                role="Code Reviewer",
                organization="Loveble",
                entity_type="role-instance",
                label="Hemingway (Loveble)",
                tier="reviewer",
                section="review",
                section_order=2,
                parent="oppenheimer",
                managed_org="Loveble",
            ),
            "wright": EntityEntry(
                session="wright",
                home="~/ws/core/arch/wright",
                groups=["architects"],
                figure="Frank Lloyd Wright",
                role="System Architect",
                label="Wright",
                tier="architect",
                section="architecture",
                section_order=3,
            ),
            "gaudi-wf": EntityEntry(
                session="gaudi-wf",
                home="~/ws/core/arch/WF/gaudi",
                groups=["architects"],
                figure="Antoni Gaudi",
                role="Repo Architect",
                organization="WF",
                entity_type="role-instance",
                label="Gaudi (WF)",
                tier="architect",
                section="architecture",
                section_order=3,
                parent="wright",
                managed_org="WF",
            ),
            "gaudi-loveble": EntityEntry(
                session="gaudi-loveble",
                home="~/ws/core/arch/Loveble/gaudi",
                groups=["architects"],
                figure="Antoni Gaudi",
                role="Repo Architect",
                organization="Loveble",
                entity_type="role-instance",
                label="Gaudi (Loveble)",
                tier="architect",
                section="architecture",
                section_order=3,
                parent="wright",
                managed_org="Loveble",
            ),
            "koch": EntityEntry(
                session="koch",
                home="~/ws/core/quality",
                groups=["orchestrators"],
                figure="Robert Koch",
                role="Bug Quality Architect",
                label="Koch",
                tier="quality",
                section="quality",
                section_order=4,
            ),
            "snow-wf": EntityEntry(
                session="snow-wf",
                home="~/ws/core/quality/WF/snow",
                groups=["orchestrators"],
                figure="John Snow",
                role="Bug Validator",
                organization="WF",
                entity_type="role-instance",
                label="Snow (WF)",
                tier="quality",
                section="quality",
                section_order=4,
                parent="koch",
                managed_org="WF",
            ),
            "snow-loveble": EntityEntry(
                session="snow-loveble",
                home="~/ws/core/quality/Loveble/snow",
                groups=["orchestrators"],
                figure="John Snow",
                role="Bug Validator",
                organization="Loveble",
                entity_type="role-instance",
                label="Snow (Loveble)",
                tier="quality",
                section="quality",
                section_order=4,
                parent="koch",
                managed_org="Loveble",
            ),
            "darwin": EntityEntry(
                session="darwin",
                home="~/ws/core/darwin",
                groups=["standalone"],
                figure="Charles Darwin",
                role="System Evolution Architect",
                label="Darwin",
                tier="knowledge-worker",
                section="knowledge-workers",
                section_order=5,
            ),
            "gallup": EntityEntry(
                session="gallup",
                home="~/ws/core/gallup",
                groups=["standalone"],
                figure="George Gallup",
                role="Market Research",
                label="Gallup",
                tier="knowledge-worker",
                section="knowledge-workers",
                section_order=5,
            ),
            "twain": EntityEntry(
                session="twain",
                home="~/ws/core/twain",
                groups=["standalone"],
                figure="Mark Twain",
                role="Social Content Agent",
                label="Twain",
                tier="knowledge-worker",
                section="knowledge-workers",
                section_order=5,
            ),
            "durant": EntityEntry(
                session="durant",
                home="~/ws/core/durant",
                groups=["standalone"],
                figure="Will Durant",
                role="Narrative Historian",
                label="Durant",
                tier="knowledge-worker",
                section="knowledge-workers",
                section_order=5,
            ),
        },
        repos=[
            RepoInfo(org="WF", name="agent-backbone", path="/ws/core/code/WF/agent-backbone"),
            RepoInfo(
                org="Loveble",
                name="lovely-console",
                path="/ws/core/code/Loveble/lovely-console",
            ),
            RepoInfo(org="Arclio", name="platform-api", path="/ws/core/code/Arclio/platform-api"),
        ],
    )


def _make_state_service(snapshot_map: dict[str, StateSnapshot]) -> MagicMock:
    """Create a StateService mock backed by a session snapshot map."""
    svc = MagicMock()

    async def get_state(session: str) -> StateSnapshot:
        return snapshot_map.get(
            session,
            StateSnapshot(state=AgentState.OFFLINE, source="default"),
        )

    svc.get_state = AsyncMock(side_effect=get_state)
    return svc


def _make_tmux_service(active_sessions: list[str]) -> MagicMock:
    """Create a tmux service mock with active session listing."""
    svc = MagicMock()
    svc.list_sessions = AsyncMock(return_value=active_sessions)
    return svc


def _set_di_overrides(api_app, *, state_svc: MagicMock, tmux_svc: MagicMock) -> None:
    """Install route dependency overrides for state and tmux services."""
    api_app.dependency_overrides[get_state_service] = lambda: state_svc
    api_app.dependency_overrides[get_tmux_service] = lambda: tmux_svc


def _clear_di_overrides(api_app) -> None:
    """Remove dependency overrides added by hierarchy tests."""
    api_app.dependency_overrides.pop(get_state_service, None)
    api_app.dependency_overrides.pop(get_tmux_service, None)


class TestHierarchyEndpoint:
    async def test_returns_canonical_tree_with_org_attached_coding_agents(
        self, api_app, api_client, auth_headers
    ):
        """Hierarchy response should follow the canonical sections and org placement."""
        old_config = api_app.state.config
        api_app.state.config = dc_replace(old_config, registry=_hierarchy_registry())

        state_svc = _make_state_service(
            {
                "leo": _snapshot(AgentState.BUSY),
                "ike": _snapshot(AgentState.IDLE),
                "bell-wf": _snapshot(AgentState.BUSY, issue=755),
                "bell-loveble": _snapshot(AgentState.IDLE),
                "ada": _snapshot(AgentState.PLAN_WAITING),
                "euclid-wf": _snapshot(AgentState.IDLE),
                "euclid-loveble": _snapshot(AgentState.STARTING),
                "darwin": _snapshot(AgentState.IDLE),
                "agent-backbone": _snapshot(AgentState.IDLE, issue=755),
                "lovely-console": _snapshot(AgentState.IDLE),
                "platform-api": _snapshot(AgentState.STARTING),
            }
        )
        tmux_svc = _make_tmux_service(
            [
                "leo",
                "ike",
                "bell-wf",
                "bell-loveble",
                "ada",
                "euclid-wf",
                "euclid-loveble",
                "darwin",
                "agent-backbone",
                "platform-api",
            ]
        )
        _set_di_overrides(api_app, state_svc=state_svc, tmux_svc=tmux_svc)

        try:
            resp = await api_client.get("/api/hierarchy", headers=auth_headers)
        finally:
            _clear_di_overrides(api_app)
            api_app.state.config = old_config

        assert resp.status_code == 200
        data = resp.json()

        assert data["root"] == {
            "id": "jarvis",
            "label": "Jarvis",
            "role": "Personal Assistant",
            "tier": "assistant",
            "state": "offline",
            "session": None,
            "online": False,
            "managedOrg": None,
            "children": None,
            "codingAgents": None,
        }
        assert data["strategy"]["id"] == "leo"
        assert data["strategy"]["label"] == "Da Vinci"
        assert data["strategy"]["state"] == "busy"

        assert [node["id"] for node in data["independentPeers"]] == ["feynman", "brunel"]
        assert [section["id"] for section in data["sections"]] == [
            "operations",
            "review",
            "architecture",
            "quality",
            "knowledge-workers",
        ]

        operations = data["sections"][0]["nodes"]
        ike = next(node for node in operations if node["id"] == "ike")
        assert ike["label"] == "Eisenhower"
        assert ike["state"] == "idle"

        bell_wf = next(node for node in ike["children"] if node["id"] == "bell-wf")
        assert bell_wf["managedOrg"] == "WF"
        assert bell_wf["state"] == "busy"
        assert bell_wf["codingAgents"] == [
            {
                "id": "agent-backbone",
                "label": "agent-backbone",
                "org": "WF",
                "state": "idle",
                "online": True,
                "currentIssue": 755,
                "swarmWorkers": None,
            }
        ]

        bell_loveble = next(node for node in ike["children"] if node["id"] == "bell-loveble")
        assert bell_loveble["managedOrg"] == "Loveble"
        assert bell_loveble["codingAgents"] == [
            {
                "id": "lovely-console",
                "label": "lovely-console",
                "org": "Loveble",
                "state": "offline",
                "online": False,
                "currentIssue": None,
                "swarmWorkers": None,
            }
        ]

        ada = next(node for node in operations if node["id"] == "ada")
        assert ada["state"] == "plan_waiting"
        euclid_wf = next(node for node in ada["children"] if node["id"] == "euclid-wf")
        assert euclid_wf["codingAgents"] is None

        quality = data["sections"][3]
        assert quality["id"] == "quality"
        koch = quality["nodes"][0]
        assert koch["id"] == "koch"
        assert koch["tier"] == "quality"
        assert [child["id"] for child in koch["children"]] == ["snow-wf", "snow-loveble"]
        assert koch["children"][0]["managedOrg"] == "WF"
        assert koch["children"][1]["managedOrg"] == "Loveble"

        knowledge_workers = data["sections"][4]["nodes"]
        assert [node["id"] for node in knowledge_workers] == [
            "darwin",
            "gallup",
            "twain",
            "durant",
        ]

        assert data["unassignedCodingAgents"] == [
            {
                "id": "platform-api",
                "label": "platform-api",
                "org": "Arclio",
                "state": "starting",
                "online": True,
                "currentIssue": None,
                "swarmWorkers": None,
            }
        ]

    async def test_attaches_active_swarm_workers_under_parent_coding_agent(
        self, api_app, api_client, auth_headers
    ):
        """Active swarm workers should appear under their owning coding agent."""
        old_config = api_app.state.config
        api_app.state.config = dc_replace(old_config, registry=_hierarchy_registry())

        swarm_id = await api_app.state.db.create_swarm(
            repo="agent-backbone",
            task_id="755",
            coding_agent_session="agent-backbone",
            workers=[
                {
                    "name": "lead-worker",
                    "role": "lead",
                    "branch": "swarm/755/lead-worker",
                    "worktree_path": "/tmp/lead-worker",
                    "session": "lead-worker",
                },
                {
                    "name": "tester-worker",
                    "role": "tester",
                    "branch": "swarm/755/tester-worker",
                    "worktree_path": "/tmp/tester-worker",
                    "session": "tester-worker",
                },
            ],
        )
        assert swarm_id

        state_svc = _make_state_service(
            {
                "agent-backbone": _snapshot(AgentState.IDLE, issue=755),
            }
        )
        tmux_svc = _make_tmux_service(["agent-backbone", "lead-worker", "tester-worker"])
        _set_di_overrides(api_app, state_svc=state_svc, tmux_svc=tmux_svc)

        try:
            resp = await api_client.get("/api/hierarchy", headers=auth_headers)
        finally:
            _clear_di_overrides(api_app)
            api_app.state.config = old_config

        assert resp.status_code == 200
        data = resp.json()
        bell_wf = next(
            node for node in data["sections"][0]["nodes"][0]["children"] if node["id"] == "bell-wf"
        )
        agent_backbone = bell_wf["codingAgents"][0]
        workers = agent_backbone["swarmWorkers"]

        assert [worker["name"] for worker in workers] == ["lead-worker", "tester-worker"]
        assert [worker["role"] for worker in workers] == ["lead", "tester"]
        assert [worker["session"] for worker in workers] == ["lead-worker", "tester-worker"]
        assert [worker["branch"] for worker in workers] == [
            "swarm/755/lead-worker",
            "swarm/755/tester-worker",
        ]
        assert [worker["status"] for worker in workers] == ["pending", "pending"]
        assert all(isinstance(worker["id"], str) and worker["id"] for worker in workers)

    async def test_requires_auth(self, api_app, api_client, api_key):
        """Hierarchy endpoint is protected like the rest of /api/*."""
        old_config = api_app.state.config
        api_app.state.config = dc_replace(old_config, registry=_hierarchy_registry())
        _set_di_overrides(
            api_app,
            state_svc=_make_state_service({}),
            tmux_svc=_make_tmux_service([]),
        )

        try:
            resp = await api_client.get("/api/hierarchy")
        finally:
            _clear_di_overrides(api_app)
            api_app.state.config = old_config

        assert resp.status_code == 401


class TestRegistryEndpoints:
    async def test_list_entities(self, api_app, api_client, auth_headers):
        """GET /api/registry/entities returns all registry entities."""
        old_config = api_app.state.config
        api_app.state.config = dc_replace(old_config, registry=_hierarchy_registry())
        try:
            resp = await api_client.get("/api/registry/entities", headers=auth_headers)
        finally:
            api_app.state.config = old_config
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 23  # 17 original + 6 new
        ids = {item["id"] for item in data["items"]}
        assert "ike" in ids
        assert "jarvis" in ids

    async def test_list_repos(self, api_app, api_client, auth_headers):
        """GET /api/registry/repos returns repos sorted by org/name."""
        old_config = api_app.state.config
        api_app.state.config = dc_replace(old_config, registry=_hierarchy_registry())
        try:
            resp = await api_client.get("/api/registry/repos", headers=auth_headers)
        finally:
            api_app.state.config = old_config
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        orgs = [item["org"] for item in data["items"]]
        assert orgs == sorted(orgs, key=str.casefold)

    async def test_registry_requires_auth(self, api_app, api_client, api_key):
        """Registry endpoints require authentication."""
        resp = await api_client.get("/api/registry/entities")
        assert resp.status_code == 401
