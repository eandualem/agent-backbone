"""Registry data models — entity entries, repo info, and the registry container."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path


@dataclass(frozen=True)
class EntityEntry:
    """A single entity from entity-registry.json."""

    session: str
    home: str
    groups: list[str]
    figure: str
    role: str
    organization: str = ""
    entity_type: str = "agent"


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
            entry.session: str(Path(entry.home).expanduser()) for entry in self.entities.values()
        }

    @cached_property
    def repo_path_by_name(self) -> dict[str, str]:
        """Repo name -> filesystem path."""
        return {r.name: r.path for r in self.repos}
