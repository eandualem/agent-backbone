"""Dynamic entity registry — loads entities from JSON, discovers coding repos from filesystem.

Replaces hardcoded _DEFAULT_SESSIONS, _ENTITY_DIRS, _CODE_BASE_DIRS, _ENTITY_DISPLAY,
and KNOWN_ORGS with a single registry built from ~/.claude/state/entity-registry.json
and filesystem scanning of ~/ws/core/code/*/.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EntityEntry:
    """A single entity from entity-registry.json."""

    session: str
    home: str
    groups: list[str]
    figure: str
    role: str
    organization: str = ""


@dataclass(frozen=True)
class RepoInfo:
    """A discovered coding repository."""

    org: str
    name: str
    path: str


@dataclass
class EntityRegistry:
    """Combined entity + repo registry. Built once per config load."""

    entities: dict[str, EntityEntry] = field(default_factory=dict)
    repos: list[RepoInfo] = field(default_factory=list)

    @cached_property
    def sessions_map(self) -> dict[str, str]:
        """Entity name -> session name mapping."""
        return {name: entry.session for name, entry in self.entities.items()}

    @cached_property
    def entity_by_session(self) -> dict[str, str]:
        """Session name -> entity name (reverse lookup)."""
        return {entry.session: name for name, entry in self.entities.items()}

    @cached_property
    def all_entities(self) -> list[str]:
        """List of all entity names."""
        return list(self.entities.keys())

    @cached_property
    def repo_names(self) -> frozenset[str]:
        """Set of all discovered repo names."""
        return frozenset(r.name for r in self.repos)

    @cached_property
    def orgs(self) -> frozenset[str]:
        """Set of all discovered org names."""
        return frozenset(r.org for r in self.repos)

    @cached_property
    def home_by_session(self) -> dict[str, str]:
        """Session name -> home directory (expanded)."""
        return {
            entry.session: str(Path(entry.home).expanduser())
            for entry in self.entities.values()
        }

    @cached_property
    def repo_path_by_name(self) -> dict[str, str]:
        """Repo name -> filesystem path."""
        return {r.name: r.path for r in self.repos}


def load_entity_registry(path: Path) -> dict[str, EntityEntry]:
    """Read entity-registry.json and return entity dict.

    Raises FileNotFoundError if the file doesn't exist.
    Raises ValueError on malformed JSON.
    """
    raw = json.loads(path.read_text())
    entities: dict[str, EntityEntry] = {}
    for name, data in raw.items():
        entities[name] = EntityEntry(
            session=data["session"],
            home=data["home"],
            groups=data.get("groups", []),
            figure=data.get("figure", ""),
            role=data.get("role", ""),
            organization=data.get("organization", ""),
        )
    return entities


def discover_coding_repos(base_dir: Path) -> list[RepoInfo]:
    """Scan base_dir for org subdirectories, then repos within each org.

    Structure expected: base_dir/{OrgName}/{repo-name}/
    Skips hidden directories and non-directory entries.
    """
    repos: list[RepoInfo] = []
    if not base_dir.is_dir():
        return repos

    for org_dir in sorted(base_dir.iterdir()):
        if not org_dir.is_dir() or org_dir.name.startswith("."):
            continue
        for repo_dir in sorted(org_dir.iterdir()):
            if not repo_dir.is_dir() or repo_dir.name.startswith("."):
                continue
            repos.append(RepoInfo(
                org=org_dir.name,
                name=repo_dir.name,
                path=str(repo_dir),
            ))

    return repos


def build_registry(
    registry_path: Path,
    code_base_dir: Path,
) -> EntityRegistry:
    """Build a complete EntityRegistry from JSON file + filesystem scan.

    Raises FileNotFoundError if registry_path doesn't exist.
    """
    entities = load_entity_registry(registry_path)
    repos = discover_coding_repos(code_base_dir)
    return EntityRegistry(entities=entities, repos=repos)
