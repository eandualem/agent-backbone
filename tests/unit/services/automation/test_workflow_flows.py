"""Tests for automation workflow helper flows."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from agent_backbone.config import BackboneConfig
from agent_backbone.services.automation._flows import (
    _count_pending_evening,
    count_pending_issues,
    morning_startup,
)
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
        gh.list_open_issues.side_effect = lambda label: [object()] if label == "for:feynman" else []

        result = await count_pending_issues(config, gh)

        assert result == {"feynman": 1}
        assert [call.args[0] for call in gh.list_open_issues.await_args_list] == ["for:feynman"]

    async def test_evening_count_skips_abstract_role_targets(self):
        config = _config_with_abstract_role()
        gh = AsyncMock()
        gh.list_open_issues.side_effect = lambda label: [object()] if label == "for:feynman" else []

        result = await _count_pending_evening(config, gh)

        assert result == {"feynman": 1}
        assert [call.args[0] for call in gh.list_open_issues.await_args_list] == ["for:feynman"]


class TestMorningStartup:
    async def test_does_not_start_agent_sessions(self):
        config = BackboneConfig(webhook_secret="test")
        gh = AsyncMock()

        with patch("agent_backbone.services.automation._flows.get_config", return_value=config):
            with patch("agent_backbone.services.automation._flows.get_gh", return_value=gh):
                with patch(
                    "agent_backbone.services.automation._flows.count_pending_issues",
                    new_callable=AsyncMock,
                    return_value={"ike": 1},
                ):
                    with patch(
                        "agent_backbone.services.automation._flows.deliver_overnight_issues",
                        new_callable=AsyncMock,
                        return_value={"ike": "delivered_#1"},
                    ):
                        with patch(
                            "agent_backbone.services.automation._flows.list_sessions",
                            new_callable=AsyncMock,
                            return_value=["ike"],
                        ):
                            with patch(
                                "agent_backbone.services.automation._flows.start_session",
                                new_callable=AsyncMock,
                            ) as mock_start:
                                with patch(
                                    "agent_backbone.services.automation._flows.format_digest",
                                    return_value="digest",
                                ):
                                    result = await morning_startup()

        assert result["started"] == []
        mock_start.assert_not_awaited()
