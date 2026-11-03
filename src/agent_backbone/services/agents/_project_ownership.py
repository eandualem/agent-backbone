"""Internal project ownership for a captured provider; no startup installs it yet."""

from collections.abc import Callable
from pathlib import Path

from agent_backbone.services.database import ProfileProvider, ProfileRegistry
from agent_backbone.skills import project_resources

_PROVIDER: ProfileProvider | None = None


async def install_project_ownership(provider: ProfileProvider) -> None:
    """Pin the reconciled identity once, without adopting existing project links."""
    global _PROVIDER
    await provider.check()
    if _PROVIDER is not None and provider != _PROVIDER:
        raise ValueError("Project ownership is already pinned")
    _PROVIDER = provider


def project_claims(data_dir: Path) -> Callable[[tuple[Path, ...]], None] | None:
    """Capture a claim callback; legacy installations retain their behavior.

    Registry validation runs on every invocation, including callbacks retained
    before invalidation. Local database restore and in-flight fencing remain the
    lifecycle owner's responsibility before activation.
    """
    provider = _PROVIDER
    if provider is None:
        return None
    reservation = provider.reservation
    if data_dir.resolve() != reservation.data_dir:
        raise ValueError("Project ownership does not match the provider directory")

    def claim(paths: tuple[Path, ...]) -> None:
        if data_dir.resolve() != reservation.data_dir:
            raise ValueError("Project ownership does not match the provider directory")
        ProfileRegistry(provider.registry_path).claim_projects(
            reservation.identity, reservation.data_dir, paths
        )

    return claim


def claim_project_directories(data_dir: Path, directories: tuple[Path, ...]) -> None:
    if claim := project_claims(data_dir):
        claim(project_resources(directories))
