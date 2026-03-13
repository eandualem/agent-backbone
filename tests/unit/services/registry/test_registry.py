"""Tests for agent_backbone/services/registry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_backbone.services.registry import (
    EntityEntry,
    EntityInstance,
    EntityRegistry,
    RepoInfo,
    build_registry,
    discover_coding_repos,
    load_entity_registry,
)

# --- Sample data helpers ---


def _sample_registry_data() -> dict:
    """Minimal valid entity registry JSON structure."""
    return {
        "ike": {
            "session": "ike",
            "home": "~/ws/core/ike/",
            "groups": ["orchestrators"],
            "figure": "Dwight D. Eisenhower",
            "role": "Core Orchestrator",
        },
        "leo": {
            "session": "leo",
            "home": "~/ws/leo/",
            "groups": ["strategy"],
            "figure": "Leonardo da Vinci",
            "role": "Strategy Co-Architect",
        },
    }


def _write_registry_json(path: Path, data: dict) -> Path:
    """Write registry data to a JSON file and return the path."""
    path.write_text(json.dumps(data))
    return path


# --- load_entity_registry ---


class TestLoadEntityRegistry:
    def test_loads_valid_json_with_multiple_entities(self, tmp_path):
        data = _sample_registry_data()
        registry_file = _write_registry_json(tmp_path / "entities.json", data)

        entities = load_entity_registry(registry_file)

        assert len(entities) == 2
        assert "ike" in entities
        assert "leo" in entities
        assert isinstance(entities["ike"], EntityEntry)
        assert entities["ike"].session == "ike"
        assert entities["ike"].home == "~/ws/core/ike/"
        assert entities["ike"].groups == ["orchestrators"]
        assert entities["ike"].figure == "Dwight D. Eisenhower"
        assert entities["ike"].role == "Core Orchestrator"
        assert entities["leo"].session == "leo"
        assert entities["leo"].home == "~/ws/leo/"

    def test_defaults_for_optional_fields(self, tmp_path):
        data = {
            "minimal": {
                "session": "min-session",
                "home": "~/ws/minimal/",
            },
        }
        registry_file = _write_registry_json(tmp_path / "entities.json", data)

        entities = load_entity_registry(registry_file)

        assert entities["minimal"].groups == []
        assert entities["minimal"].figure == ""
        assert entities["minimal"].role == ""
        assert entities["minimal"].entity_type == "agent"

    def test_reads_type_field_as_entity_type(self, tmp_path):
        data = {
            "jarvis": {
                "session": None,
                "home": "~/ws/jarvis/",
                "type": "service",
                "figure": "Jarvis",
                "role": "Personal Assistant",
            },
            "ike": {
                "session": "ike",
                "home": "~/ws/core/ike/",
                "type": "agent",
                "figure": "Dwight D. Eisenhower",
                "role": "Core Orchestrator",
            },
        }
        registry_file = _write_registry_json(tmp_path / "entities.json", data)

        entities = load_entity_registry(registry_file)

        assert entities["jarvis"].entity_type == "service"
        assert entities["ike"].entity_type == "agent"

    def test_loads_role_entry_with_instances(self, tmp_path):
        data = {
            "bell": {
                "type": "role",
                "figure": "Alexander Graham Bell",
                "role": "Org Orchestrator",
                "groups": ["orchestrators"],
                "roleDefinition": "~/orchestration/roles/bell/",
                "instances": {
                    "wf": {
                        "home": "~/ws/core/code/WF/bell",
                        "session": "bell-wf",
                        "organization": "WF",
                    },
                    "loveble": {
                        "home": "~/ws/core/code/Loveble/bell",
                        "session": "bell-loveble",
                        "organization": "Loveble",
                    },
                },
            }
        }
        registry_file = _write_registry_json(tmp_path / "entities.json", data)

        entities = load_entity_registry(registry_file)

        assert entities["bell"].entity_type == "role"
        assert entities["bell"].session is None
        assert entities["bell"].home == "~/ws/core/code/WF/bell"
        assert entities["bell"].role_definition == "~/orchestration/roles/bell/"
        assert entities["bell"].instances["wf"].session == "bell-wf"
        assert entities["bell"].instances["wf"].organization == "WF"
        assert entities["bell"].instances["loveble"].session == "bell-loveble"

    def test_keeps_flat_role_instances_concrete(self, tmp_path):
        data = {
            "bell-wf": {
                "session": "bell-wf",
                "home": "~/ws/core/code/WF/bell",
                "groups": ["orchestrators"],
                "figure": "Alexander Graham Bell",
                "role": "Org Orchestrator",
                "organization": "WF",
                "type": "role-instance",
                "roleDefinition": "~/orchestration/roles/bell/",
                "roleEntity": "bell",
            },
            "bell-loveble": {
                "session": "bell-loveble",
                "home": "~/ws/core/code/Loveble/bell",
                "groups": ["orchestrators"],
                "figure": "Alexander Graham Bell",
                "role": "Org Orchestrator",
                "organization": "Loveble",
                "type": "role-instance",
                "roleDefinition": "~/orchestration/roles/bell/",
                "roleEntity": "bell",
            },
        }
        registry_file = _write_registry_json(tmp_path / "entities.json", data)

        entities = load_entity_registry(registry_file)

        assert "bell" not in entities
        assert entities["bell-wf"].entity_type == "role-instance"

    def test_raises_file_not_found_on_missing_file(self, tmp_path):
        missing = tmp_path / "does_not_exist.json"
        with pytest.raises(FileNotFoundError):
            load_entity_registry(missing)

    def test_raises_value_error_on_malformed_json(self, tmp_path):
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("{not valid json")
        with pytest.raises(json.JSONDecodeError):
            load_entity_registry(bad_file)


# --- discover_coding_repos ---


class TestDiscoverCodingRepos:
    def test_discovers_repos_in_multiple_org_dirs(self, tmp_path):
        # Structure: base_dir/Arclio/platform-api/
        #            base_dir/Arclio/arclio-assistant/
        #            base_dir/WF/agent-backbone/
        (tmp_path / "Arclio" / "platform-api").mkdir(parents=True)
        (tmp_path / "Arclio" / "arclio-assistant").mkdir(parents=True)
        (tmp_path / "WF" / "agent-backbone").mkdir(parents=True)

        repos = discover_coding_repos(tmp_path)

        assert len(repos) == 3
        names = {r.name for r in repos}
        assert names == {"platform-api", "arclio-assistant", "agent-backbone"}
        orgs = {r.org for r in repos}
        assert orgs == {"Arclio", "WF"}

        # Verify path is set correctly
        for repo in repos:
            assert repo.path == str(tmp_path / repo.org / repo.name)

    def test_empty_directory_returns_empty_list(self, tmp_path):
        repos = discover_coding_repos(tmp_path)
        assert repos == []

    def test_nonexistent_directory_returns_empty_list(self, tmp_path):
        repos = discover_coding_repos(tmp_path / "nonexistent")
        assert repos == []

    def test_skips_hidden_org_directories(self, tmp_path):
        (tmp_path / ".hidden-org" / "some-repo").mkdir(parents=True)
        (tmp_path / "Visible" / "real-repo").mkdir(parents=True)

        repos = discover_coding_repos(tmp_path)

        assert len(repos) == 1
        assert repos[0].org == "Visible"
        assert repos[0].name == "real-repo"

    def test_skips_hidden_repo_directories(self, tmp_path):
        (tmp_path / "Org" / ".hidden-repo").mkdir(parents=True)
        (tmp_path / "Org" / "visible-repo").mkdir(parents=True)

        repos = discover_coding_repos(tmp_path)

        assert len(repos) == 1
        assert repos[0].name == "visible-repo"

    def test_skips_files_in_org_level(self, tmp_path):
        # File at org level should be skipped
        (tmp_path / "not-a-dir.txt").write_text("file")
        (tmp_path / "RealOrg" / "real-repo").mkdir(parents=True)

        repos = discover_coding_repos(tmp_path)

        assert len(repos) == 1
        assert repos[0].org == "RealOrg"

    def test_skips_files_in_repo_level(self, tmp_path):
        # File inside org dir should be skipped
        (tmp_path / "Org").mkdir()
        (tmp_path / "Org" / "README.md").write_text("file")
        (tmp_path / "Org" / "real-repo").mkdir()

        repos = discover_coding_repos(tmp_path)

        assert len(repos) == 1
        assert repos[0].name == "real-repo"

    def test_results_sorted_by_org_and_name(self, tmp_path):
        (tmp_path / "Zebra" / "charlie").mkdir(parents=True)
        (tmp_path / "Zebra" / "alpha").mkdir(parents=True)
        (tmp_path / "Apple" / "beta").mkdir(parents=True)

        repos = discover_coding_repos(tmp_path)

        org_repo_pairs = [(r.org, r.name) for r in repos]
        assert org_repo_pairs == [("Apple", "beta"), ("Zebra", "alpha"), ("Zebra", "charlie")]


# --- build_registry ---


class TestBuildRegistry:
    def test_combines_entities_and_repos(self, tmp_path):
        data = _sample_registry_data()
        registry_file = _write_registry_json(tmp_path / "entities.json", data)

        code_dir = tmp_path / "code"
        (code_dir / "WF" / "agent-backbone").mkdir(parents=True)

        registry = build_registry(registry_file, code_dir)

        assert isinstance(registry, EntityRegistry)
        assert "ike" in registry.entities
        assert "leo" in registry.entities
        assert len(registry.repos) == 1
        assert registry.repos[0].name == "agent-backbone"

    def test_raises_file_not_found_when_registry_missing(self, tmp_path):
        missing = tmp_path / "missing.json"
        code_dir = tmp_path / "code"
        code_dir.mkdir()

        with pytest.raises(FileNotFoundError):
            build_registry(missing, code_dir)

    def test_works_with_empty_code_dir(self, tmp_path):
        data = _sample_registry_data()
        registry_file = _write_registry_json(tmp_path / "entities.json", data)
        code_dir = tmp_path / "code"
        code_dir.mkdir()

        registry = build_registry(registry_file, code_dir)

        assert len(registry.entities) == 2
        assert len(registry.repos) == 0

    def test_excludes_entity_home_paths_from_repo_discovery(self, tmp_path):
        data = {
            "bell-wf": {
                "session": "bell-wf",
                "home": str(tmp_path / "code" / "WF" / "bell"),
                "groups": ["orchestrators"],
                "figure": "Alexander Graham Bell",
                "role": "Org Orchestrator",
                "organization": "WF",
                "type": "role-instance",
                "roleDefinition": "~/orchestration/roles/bell/",
                "roleEntity": "bell",
            },
            "bell-loveble": {
                "session": "bell-loveble",
                "home": str(tmp_path / "code" / "Loveble" / "bell"),
                "groups": ["orchestrators"],
                "figure": "Alexander Graham Bell",
                "role": "Org Orchestrator",
                "organization": "Loveble",
                "type": "role-instance",
                "roleDefinition": "~/orchestration/roles/bell/",
                "roleEntity": "bell",
            },
        }
        registry_file = _write_registry_json(tmp_path / "entities.json", data)

        code_dir = tmp_path / "code"
        (code_dir / "WF" / "bell").mkdir(parents=True)
        (code_dir / "Loveble" / "bell").mkdir(parents=True)
        (code_dir / "WF" / "agent-backbone").mkdir(parents=True)

        registry = build_registry(registry_file, code_dir)

        assert [(repo.org, repo.name) for repo in registry.repos] == [("WF", "agent-backbone")]


# --- EntityRegistry computed properties ---


class TestEntityRegistrySessions:
    def test_sessions_map(self):
        entities = {
            "ike": EntityEntry(session="ike", home="~/ws/core/ike/", groups=[], figure="", role=""),
            "leo": EntityEntry(session="leo", home="~/ws/leo/", groups=[], figure="", role=""),
        }
        registry = EntityRegistry(entities=entities, repos=[])

        assert registry.sessions_map == {"ike": "ike", "leo": "leo"}

    def test_sessions_map_with_different_session_name(self):
        entities = {
            "coding-agent": EntityEntry(
                session="agent-backbone",
                home="~/ws/core/code/WF/agent-backbone/",
                groups=[],
                figure="",
                role="",
            ),
        }
        registry = EntityRegistry(entities=entities, repos=[])

        assert registry.sessions_map == {"coding-agent": "agent-backbone"}

    def test_sessions_map_excludes_none_sessions(self):
        entities = {
            "ike": EntityEntry(session="ike", home="~/ws/core/ike/", groups=[], figure="", role=""),
            "jarvis": EntityEntry(
                session=None,
                home="~/ws/jarvis/",
                groups=[],
                figure="Jarvis",
                role="Personal Assistant",
                entity_type="service",
            ),
        }
        registry = EntityRegistry(entities=entities, repos=[])

        assert registry.sessions_map == {"ike": "ike"}
        assert "jarvis" not in registry.sessions_map

    def test_entity_by_session(self):
        entities = {
            "ike": EntityEntry(session="ike", home="~/ws/core/ike/", groups=[], figure="", role=""),
            "leo": EntityEntry(session="leo", home="~/ws/leo/", groups=[], figure="", role=""),
        }
        registry = EntityRegistry(entities=entities, repos=[])

        assert registry.entity_by_session == {"ike": "ike", "leo": "leo"}

    def test_entity_by_session_reverse_lookup(self):
        entities = {
            "coding-agent": EntityEntry(
                session="agent-backbone",
                home="~/ws/core/code/WF/agent-backbone/",
                groups=[],
                figure="",
                role="",
            ),
        }
        registry = EntityRegistry(entities=entities, repos=[])

        assert registry.entity_by_session["agent-backbone"] == "coding-agent"

    def test_entity_by_session_excludes_none_sessions(self):
        entities = {
            "ike": EntityEntry(session="ike", home="~/ws/core/ike/", groups=[], figure="", role=""),
            "jarvis": EntityEntry(
                session=None,
                home="~/ws/jarvis/",
                groups=[],
                figure="Jarvis",
                role="Personal Assistant",
                entity_type="service",
            ),
        }
        registry = EntityRegistry(entities=entities, repos=[])

        assert registry.entity_by_session == {"ike": "ike"}
        assert None not in registry.entity_by_session

    def test_entity_by_session_includes_role_instances(self):
        entities = {
            "bell": EntityEntry(
                session=None,
                home="~/ws/core/code/WF/bell/",
                groups=["orchestrators"],
                figure="",
                role="",
                entity_type="role",
                instances={
                    "wf": EntityInstance(
                        home="~/ws/core/code/WF/bell/",
                        session="bell-wf",
                        organization="WF",
                    ),
                    "loveble": EntityInstance(
                        home="~/ws/core/code/Loveble/bell/",
                        session="bell-loveble",
                        organization="Loveble",
                    ),
                },
            )
        }
        registry = EntityRegistry(entities=entities, repos=[])

        assert "bell" not in registry.entity_by_session
        assert registry.entity_by_session["bell-wf"] == "bell-wf"
        assert registry.entity_by_session["bell-loveble"] == "bell-loveble"

    def test_entity_by_session_prefers_concrete_session_name(self):
        entities = {
            "bell": EntityEntry(
                session=None,
                home="~/ws/core/code/WF/bell/",
                groups=["orchestrators"],
                figure="",
                role="",
                entity_type="role",
                instances={
                    "wf": EntityInstance(
                        home="~/ws/core/code/WF/bell/",
                        session="bell-wf",
                        organization="WF",
                    ),
                    "loveble": EntityInstance(
                        home="~/ws/core/code/Loveble/bell/",
                        session="bell-loveble",
                        organization="Loveble",
                    ),
                },
            ),
            "bell-wf": EntityEntry(
                session="bell-wf",
                home="~/ws/core/code/WF/bell/",
                groups=["orchestrators"],
                figure="",
                role="",
                organization="WF",
                entity_type="role-instance",
            ),
        }
        registry = EntityRegistry(entities=entities, repos=[])

        assert registry.entity_by_session["bell-wf"] == "bell-wf"

    def test_concrete_sessions_map_prefers_role_instances_over_role_alias(self):
        entities = {
            "bell": EntityEntry(
                session="bell",
                home="~/ws/core/code/WF/bell/",
                groups=["orchestrators"],
                figure="Alexander Graham Bell",
                role="Org Orchestrator",
                entity_type="role",
                instances={
                    "wf": EntityInstance(
                        home="~/ws/core/code/WF/bell/",
                        session="bell-wf",
                        organization="WF",
                    ),
                    "loveble": EntityInstance(
                        home="~/ws/core/code/Loveble/bell/",
                        session="bell-loveble",
                        organization="Loveble",
                    ),
                },
            )
        }
        registry = EntityRegistry(entities=entities, repos=[])

        assert registry.concrete_sessions_map == {
            "bell-wf": "bell-wf",
            "bell-loveble": "bell-loveble",
        }
    def test_entry_for_session_materializes_role_instance(self):
        entities = {
            "bell": EntityEntry(
                session=None,
                home="~/ws/core/code/WF/bell/",
                groups=["orchestrators"],
                figure="Alexander Graham Bell",
                role="Org Orchestrator",
                entity_type="role",
                role_definition="~/orchestration/roles/bell/",
                instances={
                    "wf": EntityInstance(
                        home="~/ws/core/code/WF/bell/",
                        session="bell-wf",
                        organization="WF",
                    ),
                },
            )
        }
        registry = EntityRegistry(entities=entities, repos=[])

        entry = registry.entry_for_session("bell-wf")

        assert entry is not None
        assert entry.session == "bell-wf"
        assert entry.entity_type == "role-instance"
        assert entry.organization == "WF"
        assert entry.role_definition == "~/orchestration/roles/bell/"

    def test_delivery_sessions_for_role_returns_empty(self):
        entities = {
            "bell": EntityEntry(
                session=None,
                home="~/ws/core/code/WF/bell/",
                groups=["orchestrators"],
                figure="",
                role="",
                entity_type="role",
                instances={
                    "wf": EntityInstance(
                        home="~/ws/core/code/WF/bell/",
                        session="bell-wf",
                        organization="WF",
                    ),
                    "loveble": EntityInstance(
                        home="~/ws/core/code/Loveble/bell/",
                        session="bell-loveble",
                        organization="Loveble",
                    ),
                },
            )
        }
        registry = EntityRegistry(entities=entities, repos=[])

        assert registry.delivery_sessions_for("bell") == []


class TestEntityRegistryAllEntities:
    def test_all_entities(self):
        entities = {
            "ike": EntityEntry(
                session="ike",
                home="~/ws/core/ike/",
                groups=[],
                figure="",
                role="",
            ),
            "leo": EntityEntry(
                session="leo",
                home="~/ws/leo/",
                groups=[],
                figure="",
                role="",
            ),
            "ada": EntityEntry(
                session="ada",
                home="~/ws/core/spec/",
                groups=[],
                figure="",
                role="",
            ),
        }
        registry = EntityRegistry(entities=entities, repos=[])

        result = registry.all_entities
        assert set(result) == {"ike", "leo", "ada"}
        assert len(result) == 3

    def test_all_entities_empty(self):
        registry = EntityRegistry()
        assert registry.all_entities == []


class TestEntityRegistryRepos:
    def test_repo_names(self):
        repos = [
            RepoInfo(org="Arclio", name="platform-api", path="/ws/core/code/Arclio/platform-api"),
            RepoInfo(org="WF", name="agent-backbone", path="/ws/core/code/WF/agent-backbone"),
        ]
        registry = EntityRegistry(repos=repos)

        assert registry.repo_names == frozenset({"platform-api", "agent-backbone"})

    def test_repo_names_empty(self):
        registry = EntityRegistry()
        assert registry.repo_names == frozenset()

    def test_orgs(self):
        repos = [
            RepoInfo(org="Arclio", name="platform-api", path="/ws/core/code/Arclio/platform-api"),
            RepoInfo(
                org="Arclio",
                name="arclio-assistant",
                path="/ws/core/code/Arclio/arclio-assistant",
            ),
            RepoInfo(org="WF", name="agent-backbone", path="/ws/core/code/WF/agent-backbone"),
        ]
        registry = EntityRegistry(repos=repos)

        assert registry.orgs == frozenset({"Arclio", "WF"})

    def test_orgs_empty(self):
        registry = EntityRegistry()
        assert registry.orgs == frozenset()

    def test_repo_path_by_name(self):
        repos = [
            RepoInfo(org="Arclio", name="platform-api", path="/ws/core/code/Arclio/platform-api"),
            RepoInfo(org="WF", name="agent-backbone", path="/ws/core/code/WF/agent-backbone"),
        ]
        registry = EntityRegistry(repos=repos)

        assert registry.repo_path_by_name == {
            "platform-api": "/ws/core/code/Arclio/platform-api",
            "agent-backbone": "/ws/core/code/WF/agent-backbone",
        }


class TestEntityRegistryAddRepo:
    def test_adds_new_repo_and_updates_repo_names(self):
        repos = [
            RepoInfo(org="Arclio", name="platform-api", path="/ws/core/code/Arclio/platform-api"),
        ]
        registry = EntityRegistry(repos=repos)
        # Prime cached properties
        assert "platform-api" in registry.repo_names

        new_repo = RepoInfo(org="WF", name="new-thing", path="/ws/core/code/WF/new-thing")
        registry.add_repo(new_repo)

        assert "new-thing" in registry.repo_names
        assert "WF" in registry.orgs
        assert registry.repo_path_by_name["new-thing"] == "/ws/core/code/WF/new-thing"

    def test_dedup_does_not_duplicate(self):
        repo = RepoInfo(org="WF", name="agent-backbone", path="/ws/core/code/WF/agent-backbone")
        registry = EntityRegistry(repos=[repo])

        registry.add_repo(repo)

        assert len(registry.repos) == 1

    def test_invalidates_all_cached_properties(self):
        repos = [
            RepoInfo(org="Arclio", name="platform-api", path="/ws/core/code/Arclio/platform-api"),
        ]
        registry = EntityRegistry(repos=repos)

        # Prime all three cached properties
        _ = registry.repo_names
        _ = registry.orgs
        _ = registry.repo_path_by_name
        assert "repo_names" in registry.__dict__
        assert "orgs" in registry.__dict__
        assert "repo_path_by_name" in registry.__dict__

        new_repo = RepoInfo(org="WF", name="new-thing", path="/ws/core/code/WF/new-thing")
        registry.add_repo(new_repo)

        # Cached properties should be evicted
        assert "repo_names" not in registry.__dict__
        assert "orgs" not in registry.__dict__
        assert "repo_path_by_name" not in registry.__dict__

        # Re-access should include new repo
        assert "new-thing" in registry.repo_names
        assert "WF" in registry.orgs


class TestEntityRegistryHome:
    def test_home_by_session(self):
        entities = {
            "ike": EntityEntry(session="ike", home="~/ws/core/ike/", groups=[], figure="", role=""),
        }
        registry = EntityRegistry(entities=entities, repos=[])

        result = registry.home_by_session
        # home should be expanded (no tilde)
        assert "~" not in result["ike"]
        assert result["ike"].endswith("/ws/core/ike")

    def test_home_by_session_multiple(self):
        entities = {
            "ike": EntityEntry(session="ike", home="~/ws/core/ike/", groups=[], figure="", role=""),
            "leo": EntityEntry(session="leo", home="~/ws/leo/", groups=[], figure="", role=""),
        }
        registry = EntityRegistry(entities=entities, repos=[])

        result = registry.home_by_session
        assert "ike" in result
        assert "leo" in result
        assert result["ike"] == str(Path("~/ws/core/ike/").expanduser())
        assert result["leo"] == str(Path("~/ws/leo/").expanduser())

    def test_home_by_session_excludes_none_sessions(self):
        entities = {
            "ike": EntityEntry(session="ike", home="~/ws/core/ike/", groups=[], figure="", role=""),
            "jarvis": EntityEntry(
                session=None,
                home="~/ws/jarvis/",
                groups=[],
                figure="Jarvis",
                role="Personal Assistant",
                entity_type="service",
            ),
        }
        registry = EntityRegistry(entities=entities, repos=[])

        result = registry.home_by_session
        assert "ike" in result
        assert None not in result
        assert len(result) == 1

    def test_home_by_session_empty(self):
        registry = EntityRegistry()
        assert registry.home_by_session == {}

    def test_home_by_session_includes_role_instances(self):
        entities = {
            "bell": EntityEntry(
                session="bell",
                home="~/ws/core/code/WF/bell/",
                groups=[],
                figure="",
                role="",
                entity_type="role",
                instances={
                    "wf": EntityInstance(
                        home="~/ws/core/code/WF/bell/",
                        session="bell-wf",
                        organization="WF",
                    ),
                    "loveble": EntityInstance(
                        home="~/ws/core/code/Loveble/bell/",
                        session="bell-loveble",
                        organization="Loveble",
                    ),
                },
            )
        }
        registry = EntityRegistry(entities=entities, repos=[])

        result = registry.home_by_session
        assert result["bell"] == str(Path("~/ws/core/code/WF/bell/").expanduser())
        assert result["bell-wf"] == str(Path("~/ws/core/code/WF/bell/").expanduser())
        assert result["bell-loveble"] == str(Path("~/ws/core/code/Loveble/bell/").expanduser())


class TestOrchestratorForRepo:
    def test_role_entry_without_concrete_instance_returns_none(self):
        """Abstract role entries are ignored for orchestrator resolution."""
        entities = {
            "bell": EntityEntry(
                session=None,
                home="~/ws/core/code/WF/bell/",
                groups=["orchestrators"],
                figure="",
                role="",
                instances={
                    "wf": EntityInstance(
                        home="~/ws/core/code/WF/bell/",
                        session="bell-wf",
                        organization="WF",
                    ),
                },
                entity_type="role",
            ),
        }
        repos = [
            RepoInfo(org="WF", name="agent-backbone", path="/ws/core/code/WF/agent-backbone"),
        ]
        registry = EntityRegistry(entities=entities, repos=repos)

        assert registry.orchestrator_for_repo("agent-backbone") is None

    def test_wf_repo_returns_bell(self):
        """WF org repo resolves to bell (WF orchestrator)."""
        entities = {
            "bell": EntityEntry(
                session="bell",
                home="~/ws/core/bell/",
                groups=["orchestrators"],
                figure="",
                role="",
                organization="WF",
            ),
            "ike": EntityEntry(
                session="ike",
                home="~/ws/core/ike/",
                groups=["orchestrators"],
                figure="",
                role="",
                organization="",
            ),
        }
        repos = [
            RepoInfo(org="WF", name="agent-backbone", path="/ws/core/code/WF/agent-backbone"),
        ]
        registry = EntityRegistry(entities=entities, repos=repos)

        assert registry.orchestrator_for_repo("agent-backbone") == "bell"

    def test_flat_role_instances_return_concrete_session(self):
        entities = {
            "bell": EntityEntry(
                session="bell",
                home="~/ws/core/code/WF/bell/",
                groups=["orchestrators"],
                figure="",
                role="",
                entity_type="role",
                instances={
                    "wf": EntityInstance(
                        home="~/ws/core/code/WF/bell/",
                        session="bell-wf",
                        organization="WF",
                    ),
                    "loveble": EntityInstance(
                        home="~/ws/core/code/Loveble/bell/",
                        session="bell-loveble",
                        organization="Loveble",
                    ),
                },
            ),
            "bell-wf": EntityEntry(
                session="bell-wf",
                home="~/ws/core/code/WF/bell/",
                groups=["orchestrators"],
                figure="",
                role="",
                organization="WF",
                entity_type="role-instance",
            ),
            "bell-loveble": EntityEntry(
                session="bell-loveble",
                home="~/ws/core/code/Loveble/bell/",
                groups=["orchestrators"],
                figure="",
                role="",
                organization="Loveble",
                entity_type="role-instance",
            ),
        }
        repos = [
            RepoInfo(org="WF", name="agent-backbone", path="/ws/core/code/WF/agent-backbone"),
        ]
        registry = EntityRegistry(entities=entities, repos=repos)

        assert registry.orchestrator_for_repo("agent-backbone") == "bell-wf"

    def test_arclio_repo_returns_hamilton(self):
        """Arclio org repo resolves to hamilton."""
        entities = {
            "hamilton": EntityEntry(
                session="hamilton",
                home="~/ws/core/hamilton/",
                groups=["orchestrators"],
                figure="",
                role="",
                organization="Arclio",
            ),
            "bell": EntityEntry(
                session="bell",
                home="~/ws/core/bell/",
                groups=["orchestrators"],
                figure="",
                role="",
                organization="WF",
            ),
        }
        repos = [
            RepoInfo(org="Arclio", name="platform-api", path="/ws/core/code/Arclio/platform-api"),
        ]
        registry = EntityRegistry(entities=entities, repos=repos)

        assert registry.orchestrator_for_repo("platform-api") == "hamilton"

    def test_unknown_repo_returns_none(self):
        """Repo not in registry returns None."""
        entities = {
            "bell": EntityEntry(
                session="bell",
                home="~/ws/core/bell/",
                groups=["orchestrators"],
                figure="",
                role="",
                organization="WF",
            ),
        }
        registry = EntityRegistry(entities=entities, repos=[])

        assert registry.orchestrator_for_repo("nonexistent") is None

    def test_no_orchestrator_for_org_returns_none(self):
        """Repo exists but no orchestrator entity for that org."""
        entities = {
            "bell": EntityEntry(
                session="bell",
                home="~/ws/core/bell/",
                groups=["orchestrators"],
                figure="",
                role="",
                organization="WF",
            ),
        }
        repos = [
            RepoInfo(org="Arclio", name="platform-api", path="/ws/core/code/Arclio/platform-api"),
        ]
        registry = EntityRegistry(entities=entities, repos=repos)

        assert registry.orchestrator_for_repo("platform-api") is None

    def test_organization_for_repo_returns_unique_org(self):
        entities = {}
        repos = [
            RepoInfo(org="WF", name="agent-backbone", path="/ws/core/code/WF/agent-backbone"),
        ]
        registry = EntityRegistry(entities=entities, repos=repos)

        assert registry.organization_for_repo("agent-backbone") == "WF"

    def test_organization_for_repo_returns_none_when_ambiguous(self):
        entities = {}
        repos = [
            RepoInfo(org="WF", name="shared-repo", path="/ws/core/code/WF/shared-repo"),
            RepoInfo(
                org="Loveble",
                name="shared-repo",
                path="/ws/core/code/Loveble/shared-repo",
            ),
        ]
        registry = EntityRegistry(entities=entities, repos=repos)

        assert registry.organization_for_repo("shared-repo") is None
