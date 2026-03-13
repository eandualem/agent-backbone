"""Entity registry loading — JSON file parsing and filesystem discovery."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from agent_backbone.services.registry.models import (
    EntityEntry,
    EntityInstance,
    EntityRegistry,
    RepoInfo,
)

log = logging.getLogger(__name__)


def _load_instances(data: dict) -> dict[str, EntityInstance]:
    """Parse optional role-instance definitions from the registry payload."""
    raw_instances = data.get("instances", {})
    if not isinstance(raw_instances, dict):
        return {}

    instances: dict[str, EntityInstance] = {}
    for name, instance in raw_instances.items():
        if not isinstance(instance, dict):
            continue
        instances[name] = EntityInstance(
            home=instance.get("home", ""),
            session=instance.get("session"),
            organization=instance.get("organization", ""),
        )
    return instances


def _default_session(name: str, data: dict, instances: dict[str, EntityInstance]) -> str | None:
    """Derive a stable entity session from a registry entry."""
    session = data.get("session")
    if session is not None:
        return session

    if data.get("type") == "role":
        return None

    if len(instances) == 1:
        return next(iter(instances.values())).session

    return None


def _default_home(data: dict, instances: dict[str, EntityInstance]) -> str:
    """Derive a stable home path from flat or role-based registry entries."""
    home = data.get("home", "")
    if home:
        return home
    if instances:
        first = next(iter(instances.values()))
        if first.home:
            return first.home
    return data.get("roleDefinition", "")


def _entity_home_paths(entities: dict[str, EntityEntry]) -> set[Path]:
    """Collect entity home directories that should not be treated as coding repos."""
    excluded: set[Path] = set()
    for entry in entities.values():
        if entry.home:
            excluded.add(Path(entry.home).expanduser().resolve(strict=False))
        for instance in entry.instances.values():
            if instance.home:
                excluded.add(Path(instance.home).expanduser().resolve(strict=False))
    return excluded


def load_entity_registry(path: Path) -> dict[str, EntityEntry]:
    """Read entity-registry.json and return entity dict.

    Raises FileNotFoundError if the file doesn't exist.
    Raises ValueError on malformed JSON.
    """
    raw = json.loads(path.read_text())
    entities: dict[str, EntityEntry] = {}
    for name, data in raw.items():
        instances = _load_instances(data)
        entities[name] = EntityEntry(
            session=_default_session(name, data, instances),
            home=_default_home(data, instances),
            groups=data.get("groups", []),
            figure=data.get("figure", ""),
            role=data.get("role", ""),
            organization=data.get("organization", ""),
            entity_type=data.get("type", "agent"),
            role_definition=data.get("roleDefinition", ""),
            instances=instances,
        )
    return entities


def discover_coding_repos(
    base_dir: Path,
    *,
    exclude_paths: set[Path] | None = None,
) -> list[RepoInfo]:
    """Scan base_dir for org subdirectories, then repos within each org.

    Structure expected: base_dir/{OrgName}/{repo-name}/
    Skips hidden directories, non-directory entries, and entity home paths.
    """
    repos: list[RepoInfo] = []
    if not base_dir.is_dir():
        return repos
    normalized_excludes = {
        path.expanduser().resolve(strict=False) for path in (exclude_paths or set())
    }

    for org_dir in sorted(base_dir.iterdir()):
        if not org_dir.is_dir() or org_dir.name.startswith("."):
            continue
        for repo_dir in sorted(org_dir.iterdir()):
            if not repo_dir.is_dir() or repo_dir.name.startswith("."):
                continue
            if repo_dir.resolve(strict=False) in normalized_excludes:
                log.debug("Skipping entity home during repo discovery: %s", repo_dir)
                continue
            repos.append(
                RepoInfo(
                    org=org_dir.name,
                    name=repo_dir.name,
                    path=str(repo_dir),
                )
            )

    return repos


def build_registry(
    registry_path: Path,
    code_base_dir: Path,
) -> EntityRegistry:
    """Build a complete EntityRegistry from JSON file + filesystem scan.

    Raises FileNotFoundError if registry_path doesn't exist.
    """
    entities = load_entity_registry(registry_path)
    repos = discover_coding_repos(code_base_dir, exclude_paths=_entity_home_paths(entities))
    return EntityRegistry(entities=entities, repos=repos)
