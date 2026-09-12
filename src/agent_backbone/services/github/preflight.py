"""Lazy authenticated checks for launchers, independent of GitHub event intake."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from agent_backbone.services.github.interface import GitHubClient

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig


def actions_checker(config: BackboneConfig) -> Callable[[str], Awaitable[bool]]:
    """Use Backbone's token/App credentials only when a repository needs checking."""

    async def check(repo: str) -> bool:
        async with GitHubClient(config) as client:
            return await client.actions_enabled(repo)

    return check
