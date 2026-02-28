"""Entity registry loading — JSON file parsing and filesystem discovery."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from agent_backbone.services.registry.models import EntityEntry, EntityRegistry, RepoInfo

log = logging.getLogger(__name__)


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
            entity_type=data.get("type", "agent"),
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
    repos = discover_coding_repos(code_base_dir)
    return EntityRegistry(entities=entities, repos=repos)
