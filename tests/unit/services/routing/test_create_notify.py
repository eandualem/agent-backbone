"""Tests for routing/_create_notify.py — create_and_notify helper."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.models import IssueData, ParsedLabels
from agent_backbone.services.routing import create_and_notify, format_issue_notification
from tests.conftest import TEST_REPO

_CN = "agent_backbone.services.routing._create_notify"


def _make_issue(number: int = 99, targets: list[str] | None = None) -> IssueData:
    return IssueData(
        number=number,
        title="[task] Test issue",
        state="open",
        labels=ParsedLabels(sender="backbone", targets=targets or ["brunel"], issue_type="task"),
        html_url=f"https://github.com/{TEST_REPO}/issues/{number}",
        repo_full_name=TEST_REPO,
    )


def _patch_deliver(outcome: str = "delivered"):
    return patch(f"{_CN}.safe_deliver", new_callable=AsyncMock, return_value=outcome)


def _patch_queue(numbers: list[int]):
    return patch(
        f"{_CN}.list_open_queue_for_target",
        new_callable=AsyncMock,
        return_value=[IssueData(number=n) for n in numbers],
    )


class TestCreateAndNotify:
    async def test_creates_issue_and_notifies_target(self, config):
        mock_gh = AsyncMock()
        mock_gh.create_issue.return_value = _make_issue(99, ["brunel"])

        with _patch_queue([99]), _patch_deliver() as mock_deliver:
            result = await create_and_notify(
                mock_gh,
                title="[task] Test issue",
                body="## Context\nTest",
                labels=["from:backbone", "for:brunel", "task"],
                config=config,
                repo=TEST_REPO,
                flow_name="test",
            )

        assert result.number == 99
        mock_gh.create_issue.assert_called_once_with(
            "[task] Test issue",
            "## Context\nTest",
            ["from:backbone", "for:brunel", "task"],
            repo_full_name=TEST_REPO,
        )
        mock_deliver.assert_called_once()
        kwargs = mock_deliver.call_args.kwargs
        assert kwargs["issue_number"] == 99
        assert kwargs["target_entity"] == "brunel"
        assert kwargs["flow_name"] == "test"
        assert kwargs["enforce_issue_queue"] is True
        assert kwargs["repo"] == TEST_REPO
        assert kwargs["queue_scope"] == {("", 99)}

    async def test_multiple_targets(self, config):
        mock_gh = AsyncMock()
        mock_gh.create_issue.return_value = _make_issue(50, ["brunel", "leo"])

        with _patch_queue([50]), _patch_deliver() as mock_deliver:
            await create_and_notify(
                mock_gh,
                title="[task] Multi",
                body="body",
                labels=["from:backbone", "for:brunel", "for:leo", "task"],
                config=config,
                repo=TEST_REPO,
            )

        assert {c.kwargs["target_entity"] for c in mock_deliver.call_args_list} == {"brunel", "leo"}

    async def test_rejects_unknown_target(self, config):
        mock_gh = AsyncMock()
        with pytest.raises(ValueError, match="unknown issue target"):
            await create_and_notify(
                mock_gh,
                title="[task] Unknown target",
                body="body",
                labels=["from:backbone", "for:unknown-entity", "task"],
                config=config,
                repo=TEST_REPO,
            )
        mock_gh.create_issue.assert_not_called()

    async def test_no_for_labels_skips_notification(self, config):
        mock_gh = AsyncMock()
        mock_gh.create_issue.return_value = _make_issue(70, [])
        with _patch_deliver() as mock_deliver:
            result = await create_and_notify(
                mock_gh,
                title="[task] No targets",
                body="body",
                labels=["task"],
                config=config,
                repo=TEST_REPO,
            )
        assert result.number == 70
        mock_deliver.assert_not_called()

    async def test_db_passed_through(self, config):
        mock_gh = AsyncMock()
        mock_gh.create_issue.return_value = _make_issue(91, ["brunel"])
        mock_db = AsyncMock()
        with _patch_queue([]), _patch_deliver() as mock_deliver:
            await create_and_notify(
                mock_gh,
                title="[task] With DB",
                body="body",
                labels=["for:brunel"],
                config=config,
                repo=TEST_REPO,
                db=mock_db,
            )
        assert mock_deliver.call_args.kwargs["db"] is mock_db

    async def test_notification_uses_format_issue_notification(self, config):
        created = _make_issue(100, ["brunel"])
        mock_gh = AsyncMock()
        mock_gh.create_issue.return_value = created
        with _patch_queue([]), _patch_deliver() as mock_deliver:
            await create_and_notify(
                mock_gh,
                title="[task] Format check",
                body="body",
                labels=["for:brunel"],
                config=config,
                repo=TEST_REPO,
            )
        assert mock_deliver.call_args.args[1] == format_issue_notification(created)
