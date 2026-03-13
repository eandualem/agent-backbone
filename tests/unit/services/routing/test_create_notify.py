"""Tests for delivery/_create_notify.py — create_and_notify helper."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.config import BackboneConfig
from agent_backbone.models import IssueData, ParsedLabels
from agent_backbone.services.registry import EntityEntry, EntityRegistry
from agent_backbone.services.routing import create_and_notify


def _make_issue(number: int = 99, targets: list[str] | None = None) -> IssueData:
    targets = targets or ["brunel"]
    return IssueData(
        number=number,
        title="[task] Test issue",
        state="open",
        labels=ParsedLabels(
            sender="coding-agent",
            targets=targets,
            issue_type="task",
        ),
        html_url=f"https://github.com/eandualem/orchestration/issues/{number}",
    )


class TestCreateAndNotify:
    async def test_creates_issue_and_notifies_target(self, config):
        """Creates issue via gh and calls safe_deliver for each for: target."""
        created = _make_issue(99, ["brunel"])
        mock_gh = AsyncMock()
        mock_gh.create_issue.return_value = created

        with (
            patch(
                "agent_backbone.services.routing._create_notify.resolve_entity_sessions",
                new_callable=AsyncMock,
                return_value=["brunel"],
            ) as mock_resolve,
            patch(
                "agent_backbone.services.routing._create_notify.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
        ):
            result = await create_and_notify(
                mock_gh,
                title="[task] Test issue",
                body="## Context\nTest",
                labels=["from:coding-agent", "for:brunel", "task"],
                config=config,
                flow_name="test",
            )

        assert result.number == 99
        mock_gh.create_issue.assert_called_once_with(
            "[task] Test issue", "## Context\nTest", ["from:coding-agent", "for:brunel", "task"]
        )
        mock_resolve.assert_called_once_with("brunel", config, "[task] Test issue")
        mock_deliver.assert_called_once()
        call_kwargs = mock_deliver.call_args.kwargs
        assert call_kwargs["issue_number"] == 99
        assert call_kwargs["target_entity"] == "brunel"
        assert call_kwargs["flow_name"] == "test"

    async def test_multiple_targets(self, config):
        """Notifies each for: target independently."""
        created = _make_issue(50, ["brunel", "leo"])
        mock_gh = AsyncMock()
        mock_gh.create_issue.return_value = created

        session_map = {"brunel": "brunel", "leo": "leo"}

        async def _resolve(target, cfg, title):
            session = session_map.get(target)
            return [session] if session else []

        with (
            patch(
                "agent_backbone.services.routing._create_notify.resolve_entity_sessions",
                side_effect=_resolve,
            ),
            patch(
                "agent_backbone.services.routing._create_notify.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
        ):
            result = await create_and_notify(
                mock_gh,
                title="[task] Multi",
                body="body",
                labels=["from:backbone", "for:brunel", "for:leo", "task"],
                config=config,
            )

        assert result.number == 50
        assert mock_deliver.call_count == 2
        delivered_targets = {call.kwargs["target_entity"] for call in mock_deliver.call_args_list}
        assert delivered_targets == {"brunel", "leo"}

    async def test_rejects_unknown_target(self, config):
        """Unknown targets now fail fast during issue creation."""
        mock_gh = AsyncMock()

        with pytest.raises(ValueError, match="invalid issue target 'unknown-entity'"):
            await create_and_notify(
                mock_gh,
                title="[task] Unknown target",
                body="body",
                labels=["from:backbone", "for:unknown-entity", "task"],
                config=config,
            )

        mock_gh.create_issue.assert_not_called()

    async def test_no_for_labels_skips_notification(self, config):
        """Issue with no for: labels still creates but skips notification."""
        created = _make_issue(70, [])
        mock_gh = AsyncMock()
        mock_gh.create_issue.return_value = created

        with (
            patch(
                "agent_backbone.services.routing._create_notify.safe_deliver",
                new_callable=AsyncMock,
            ) as mock_deliver,
        ):
            result = await create_and_notify(
                mock_gh,
                title="[task] No targets",
                body="body",
                labels=["from:backbone", "task"],
                config=config,
            )

        assert result.number == 70
        mock_deliver.assert_not_called()

    async def test_returns_issue_data(self, config):
        """Returns the IssueData from gh.create_issue."""
        created = _make_issue(80, ["brunel"])
        mock_gh = AsyncMock()
        mock_gh.create_issue.return_value = created

        with (
            patch(
                "agent_backbone.services.routing._create_notify.resolve_entity_sessions",
                new_callable=AsyncMock,
                return_value=["brunel"],
            ),
            patch(
                "agent_backbone.services.routing._create_notify.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ),
        ):
            result = await create_and_notify(
                mock_gh,
                title="[task] Return check",
                body="body",
                labels=["from:backbone", "for:brunel", "task"],
                config=config,
            )

        assert isinstance(result, IssueData)
        assert result.number == 80
        assert result.title == "[task] Test issue"

    async def test_works_without_db(self, config):
        """db=None is passed through to safe_deliver (its default)."""
        created = _make_issue(90, ["brunel"])
        mock_gh = AsyncMock()
        mock_gh.create_issue.return_value = created

        with (
            patch(
                "agent_backbone.services.routing._create_notify.resolve_entity_sessions",
                new_callable=AsyncMock,
                return_value=["brunel"],
            ),
            patch(
                "agent_backbone.services.routing._create_notify.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
        ):
            result = await create_and_notify(
                mock_gh,
                title="[task] No DB",
                body="body",
                labels=["from:backbone", "for:brunel", "task"],
                config=config,
            )

        assert result.number == 90
        assert mock_deliver.call_args.kwargs["db"] is None

    async def test_with_db_passed_through(self, config):
        """When db is provided, it's forwarded to safe_deliver."""
        created = _make_issue(91, ["brunel"])
        mock_gh = AsyncMock()
        mock_gh.create_issue.return_value = created
        mock_db = AsyncMock()

        with (
            patch(
                "agent_backbone.services.routing._create_notify.resolve_entity_sessions",
                new_callable=AsyncMock,
                return_value=["brunel"],
            ),
            patch(
                "agent_backbone.services.routing._create_notify.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
        ):
            result = await create_and_notify(
                mock_gh,
                title="[task] With DB",
                body="body",
                labels=["from:backbone", "for:brunel", "task"],
                config=config,
                db=mock_db,
                flow_name="test-db",
            )

        assert result.number == 91
        assert mock_deliver.call_args.kwargs["db"] is mock_db

    async def test_notification_uses_format_issue_notification(self, config):
        """Message passed to safe_deliver matches format_issue_notification output."""
        created = _make_issue(100, ["brunel"])
        mock_gh = AsyncMock()
        mock_gh.create_issue.return_value = created

        from agent_backbone.services.routing import format_issue_notification

        expected_message = format_issue_notification(created)

        with (
            patch(
                "agent_backbone.services.routing._create_notify.resolve_entity_sessions",
                new_callable=AsyncMock,
                return_value=["brunel"],
            ),
            patch(
                "agent_backbone.services.routing._create_notify.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
        ):
            await create_and_notify(
                mock_gh,
                title="[task] Format check",
                body="body",
                labels=["from:coding-agent", "for:brunel", "task"],
                config=config,
            )

        actual_message = mock_deliver.call_args.args[1]
        assert actual_message == expected_message

    async def test_rejects_invalid_shared_role_targets(self):
        config = BackboneConfig(
            webhook_secret="test-secret",
            registry=EntityRegistry(
                entities={
                    "bell-wf": EntityEntry(
                        session="bell-wf",
                        home="~/ws/core/code/WF/bell",
                        groups=["orchestrators"],
                        figure="Bell",
                        role="Org Orchestrator",
                        organization="WF",
                        entity_type="role-instance",
                    ),
                    "bell-loveble": EntityEntry(
                        session="bell-loveble",
                        home="~/ws/core/code/Loveble/bell",
                        groups=["orchestrators"],
                        figure="Bell",
                        role="Org Orchestrator",
                        organization="Loveble",
                        entity_type="role-instance",
                    ),
                }
            ),
        )
        mock_gh = AsyncMock()

        try:
            await create_and_notify(
                mock_gh,
                title="[task] Invalid legacy target",
                body="body",
                labels=["from:coding-agent", "for:bell", "task"],
                config=config,
            )
        except ValueError as exc:
            assert "invalid issue target 'bell'" in str(exc)
        else:
            raise AssertionError("Expected ValueError for invalid issue target")

        mock_gh.create_issue.assert_not_called()
