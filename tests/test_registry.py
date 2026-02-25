"""Tests for src/registry.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.registry import (
    EntityEntry,
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
                session="agent-backbone", home="~/ws/core/code/WF/agent-backbone/",
                groups=[], figure="", role="",
            ),
        }
        registry = EntityRegistry(entities=entities, repos=[])

        assert registry.sessions_map == {"coding-agent": "agent-backbone"}

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
                session="agent-backbone", home="~/ws/core/code/WF/agent-backbone/",
                groups=[], figure="", role="",
            ),
        }
        registry = EntityRegistry(entities=entities, repos=[])

        assert registry.entity_by_session["agent-backbone"] == "coding-agent"


class TestEntityRegistryAllEntities:
    def test_all_entities(self):
        entities = {
            "ike": EntityEntry(
                session="ike", home="~/ws/core/ike/",
                groups=[], figure="", role="",
            ),
            "leo": EntityEntry(
                session="leo", home="~/ws/leo/",
                groups=[], figure="", role="",
            ),
            "ada": EntityEntry(
                session="ada", home="~/ws/core/spec/",
                groups=[], figure="", role="",
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
                org="Arclio", name="arclio-assistant",
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

    def test_home_by_session_empty(self):
        registry = EntityRegistry()
        assert registry.home_by_session == {}
