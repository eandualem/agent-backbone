"""Registry data models — entity entries, repo info, and the registry container."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path


@dataclass(frozen=True)
class EntityInstance:
    """A per-organization runtime instance for a role-based entity."""

    home: str
    session: str | None
    organization: str = ""


@dataclass(frozen=True)
class EntityEntry:
    """A single entity from entity-registry.json."""

    session: str | None
    home: str
    groups: list[str]
    figure: str
    role: str
    organization: str = ""
    entity_type: str = "agent"
    role_definition: str = ""
    instances: dict[str, EntityInstance] = field(default_factory=dict)


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

    def delivery_sessions_for(self, entity_name: str) -> list[str]:
        """Concrete tmux sessions that should receive deliveries for an entity."""
        entry = self.entities.get(entity_name)
        if not entry or entry.entity_type == "role":
            if entity_name in self.repo_names:
                return [entity_name]
            return []

        if entry.session is None:
            return []
        return [entry.session]

    def resolve_entity_sessions(self, entity_name: str) -> list[str]:
        """Resolve an entity to its concrete tmux sessions, expanding role entries."""
        entry = self.entities.get(entity_name)
        if not entry:
            if entity_name in self.repo_names:
                return [entity_name]
            return []
        if entry.entity_type == "role":
            return [
                inst.session for inst in entry.instances.values() if inst.session is not None
            ]
        if entry.session is None:
            return []
        return [entry.session]

    @cached_property
    def sessions_map(self) -> dict[str, str]:
        """Entity name -> session name mapping (excludes entities with no session)."""
        return {
            name: entry.session
            for name, entry in self.entities.items()
            if entry.session is not None
        }

    @cached_property
    def entity_by_session(self) -> dict[str, str]:
        """Session name -> entity name, including concrete role-instance sessions."""
        reverse: dict[str, str] = {}
        for name, entry in self.entities.items():
            if entry.entity_type != "role":
                continue
            for instance in entry.instances.values():
                if instance.session is not None and instance.session not in reverse:
                    reverse[instance.session] = name

        for name, entry in self.entities.items():
            if entry.session is None or entry.entity_type == "role":
                continue
            reverse[entry.session] = name
        return reverse

    @cached_property
    def concrete_sessions_map(self) -> dict[str, str]:
        """Listable entity/session pairs for concrete tmux sessions only."""
        sessions: dict[str, str] = {}
        seen_sessions: set[str] = set()

        for name, entry in self.entities.items():
            if entry.entity_type == "role":
                for instance in entry.instances.values():
                    if instance.session is None or instance.session in seen_sessions:
                        continue
                    sessions[instance.session] = instance.session
                    seen_sessions.add(instance.session)
                continue

            if entry.session is None or entry.session in seen_sessions:
                continue
            sessions[name] = entry.session
            seen_sessions.add(entry.session)

        return sessions

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
        """Session name -> home directory, including role-instance sessions."""
        homes: dict[str, str] = {}
        for entry in self.entities.values():
            if entry.session is not None:
                homes[entry.session] = str(Path(entry.home).expanduser())
            for instance in entry.instances.values():
                if instance.session is not None:
                    homes[instance.session] = str(Path(instance.home).expanduser())
        return homes

    @cached_property
    def repo_path_by_name(self) -> dict[str, str]:
        """Repo name -> filesystem path."""
        return {r.name: r.path for r in self.repos}

    @cached_property
    def tracked_sessions(self) -> dict[str, str]:
        """Logical monitor targets mapped to concrete tmux sessions.

        Includes all concrete entity sessions plus coding-repo sessions that
        are discovered from the filesystem. When an entity already maps to the
        same tmux session as a repo, the entity mapping wins to avoid duplicate
        monitor/escalation work for a single session.
        """
        tracked = dict(self.concrete_sessions_map)
        seen_sessions = set(tracked.values())

        for repo in self.repos:
            if repo.name in seen_sessions:
                continue
            tracked[repo.name] = repo.name
            seen_sessions.add(repo.name)

        return tracked

    def entry_for_session(self, session: str) -> EntityEntry | None:
        """Return the concrete entity entry for a tmux session."""
        direct_entry = self.entities.get(session)
        if (
            direct_entry is not None
            and direct_entry.session == session
            and direct_entry.entity_type != "role"
        ):
            return direct_entry

        for entry in self.entities.values():
            if entry.entity_type != "role":
                continue
            for instance in entry.instances.values():
                if instance.session != session:
                    continue
                return EntityEntry(
                    session=instance.session,
                    home=instance.home or entry.home,
                    groups=list(entry.groups),
                    figure=entry.figure,
                    role=entry.role,
                    organization=instance.organization,
                    entity_type="role-instance",
                    role_definition=entry.role_definition,
                )

        return None

    def add_repo(self, repo: RepoInfo) -> None:
        """Add a repo to the registry and invalidate cached properties."""
        if any(r.name == repo.name and r.org == repo.org for r in self.repos):
            return
        self.repos.append(repo)
        for attr in ("repo_names", "orgs", "repo_path_by_name", "tracked_sessions"):
            self.__dict__.pop(attr, None)

    def orchestrator_for_repo(self, repo_name: str) -> str | None:
        """Find the orchestrator entity for a repo by org matching.

        Returns the concrete orchestrator session/entity name or None if no match.
        """
        org = None
        for repo in self.repos:
            if repo.name == repo_name:
                org = repo.org
                break
        if not org:
            return None
        for name, entry in self.entities.items():
            if "orchestrators" not in entry.groups:
                continue
            if entry.organization == org:
                return name
            if entry.entity_type == "role":
                for inst_name, instance in entry.instances.items():
                    if instance.organization == org:
                        return instance.session or inst_name
        return None

    def organizations_for_repo(self, repo_name: str) -> frozenset[str]:
        """Return all known organizations for a repo name."""
        repo_key = repo_name.casefold()
        return frozenset(repo.org for repo in self.repos if repo.name.casefold() == repo_key)

    def organization_for_repo(self, repo_name: str) -> str | None:
        """Return the unique organization for a repo name, if it maps cleanly."""
        organizations = self.organizations_for_repo(repo_name)
        if len(organizations) != 1:
            return None
        return next(iter(organizations))
