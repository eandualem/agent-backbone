"""Tests for automation workflow helper flows."""

from __future__ import annotations

from unittest.mock import AsyncMock

from agent_backbone.config import BackboneConfig
from agent_backbone.services.automation._flows import _count_pending_evening, count_pending_issues
from agent_backbone.services.registry import EntityEntry, EntityInstance, EntityRegistry


def _config_with_abstract_role() -> BackboneConfig:
    return BackboneConfig(
        webhook_secret="test",
        registry=EntityRegistry(
            entities={
                "feynman": EntityEntry(
                    session="feynman",
                    home="~/orchestration",
                    groups=["orchestrators"],
                    figure="Richard Feynman",
                    role="Orchestrator",
                ),
                "bell": EntityEntry(
                    session=None,
                    home="~/ws/core/code/WF/bell",
                    groups=["orchestrators"],
                    figure="Alexander Graham Bell",
                    role="Org Orchestrator",
                    entity_type="role",
                    instances={
                        "wf": EntityInstance(
                            home="~/ws/core/code/WF/bell",
                            session="bell-wf",
                            organization="WF",
                        )
                    },
                ),
            },
            repos=[],
        ),
    )


class TestWorkflowPendingCounts:
    async def test_count_pending_issues_skips_abstract_role_targets(self):
        config = _config_with_abstract_role()
        gh = AsyncMock()
        gh.list_open_issues.side_effect = (
            lambda label: [object()] if label == "for:feynman" else []
        )

        result = await count_pending_issues.fn(config, gh)

        assert result == {"feynman": 1}
        assert [call.args[0] for call in gh.list_open_issues.await_args_list] == ["for:feynman"]

    async def test_evening_count_skips_abstract_role_targets(self):
        config = _config_with_abstract_role()
        gh = AsyncMock()
        gh.list_open_issues.side_effect = (
            lambda label: [object()] if label == "for:feynman" else []
        )

        result = await _count_pending_evening.fn(config, gh)

        assert result == {"feynman": 1}
        assert [call.args[0] for call in gh.list_open_issues.await_args_list] == ["for:feynman"]
