"""Tests for routing/_lifecycle.py — close-then-next."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from agent_backbone.config import AgentsConfig, AgentSpec
from agent_backbone.models import EventType, IssueData, IssueEvent, ParsedLabels
from agent_backbone.services.routing import clear as clear_dedup
from agent_backbone.services.routing import find_next_issue, on_issue_closed
from tests.conftest import TEST_REPO, make_config

_LC = "agent_backbone.services.routing._lifecycle"


def make_close_event(targets: list[str], repo_full_name: str = TEST_REPO) -> IssueEvent:
    labels = ParsedLabels(sender="ike", targets=targets, issue_type="task")
    issue = IssueData(
        number=10, title="[task] Done", state="closed", labels=labels, repo_full_name=repo_full_name
    )
    return IssueEvent(event_type=EventType.ISSUE_CLOSED, issue=issue)


def _next_issue(number: int = 11, target: str = "feynman", **kwargs) -> IssueData:
    return IssueData(
        number=number,
        title=f"[task] Next #{number}",
        labels=ParsedLabels(sender="leo", targets=[target], issue_type="task", **kwargs),
        repo_full_name=TEST_REPO,
    )


def _patch_session_exists(value: bool):
    return patch(f"{_LC}.session_exists", new_callable=AsyncMock, return_value=value)


def _patch_find_next(issue):
    return patch(f"{_LC}.find_next_issue", new_callable=AsyncMock, return_value=issue)


def _patch_deliver(outcome: str = "delivered"):
    return patch(f"{_LC}.safe_deliver", new_callable=AsyncMock, return_value=outcome)


class TestOnIssueClosed:
    def setup_method(self):
        clear_dedup()

    async def test_delivers_next_issue(self, config):
        mock_gh = AsyncMock()
        mock_gh.list_open_issues = AsyncMock(return_value=[_next_issue()])
        with _patch_session_exists(True), _patch_find_next(_next_issue()), _patch_deliver() as d:
            result = await on_issue_closed(make_close_event(["feynman"]), config, mock_gh)

        assert result["feynman"] == "delivered_#11"
        d.assert_called_once()
        assert d.await_args.kwargs["queue_scope_issue_numbers"] == {11}

    async def test_queue_empty(self, config):
        with _patch_session_exists(True), _patch_find_next(None):
            result = await on_issue_closed(make_close_event(["feynman"]), config, AsyncMock())
        assert result["feynman"] == "queue_empty"

    async def test_session_offline(self, config):
        with _patch_session_exists(False):
            result = await on_issue_closed(make_close_event(["feynman"]), config, AsyncMock())
        assert result["feynman"] == "offline"

    async def test_ignored_target_skipped(self, config):
        result = await on_issue_closed(make_close_event(["elias"]), config, AsyncMock())
        assert result["elias"] == "skipped"

    async def test_unknown_target_has_no_session(self, config):
        result = await on_issue_closed(make_close_event(["nobody"]), config, AsyncMock())
        assert result["nobody"] == "no_session"

    async def test_dedup_prevents_redelivery(self, config):
        mock_gh = AsyncMock()
        mock_gh.list_open_issues = AsyncMock(return_value=[_next_issue(6)])
        with _patch_session_exists(True), _patch_find_next(_next_issue(6)), _patch_deliver() as d:
            first = await on_issue_closed(make_close_event(["feynman"]), config, mock_gh)
            second = await on_issue_closed(make_close_event(["feynman"]), config, mock_gh)

        assert first["feynman"] == "delivered_#6"
        assert second["feynman"] == "deduped_#6"
        assert d.call_count == 1

    async def test_find_next_issue_excludes_closed_number(self, config):
        mock_gh = AsyncMock()
        mock_gh.list_open_issues = AsyncMock(return_value=[_next_issue(10), _next_issue(11)])
        result = await find_next_issue(config, "feynman", mock_gh, exclude_number=10)
        assert result is not None and result.number == 11

    async def test_find_next_issue_none_when_empty(self, config):
        mock_gh = AsyncMock()
        mock_gh.list_open_issues = AsyncMock(return_value=[])
        assert await find_next_issue(config, "feynman", mock_gh) is None

    async def test_non_default_repo_skips_dependency_hooks(self, config):
        event = make_close_event(["feynman"], repo_full_name="acme/agent-shell")
        mock_gh = AsyncMock()
        mock_gh.list_open_issues = AsyncMock(return_value=[])
        with (
            _patch_session_exists(True),
            _patch_find_next(_next_issue()) as mock_find,
            _patch_deliver(),
            patch(f"{_LC}._check_dependencies", new_callable=AsyncMock) as mock_deps,
        ):
            result = await on_issue_closed(event, config, mock_gh, db=AsyncMock())

        assert result["feynman"] == "delivered_#11"
        assert mock_find.await_args.kwargs["repo_full_name"] == "acme/agent-shell"
        mock_deps.assert_not_awaited()

    async def test_default_repo_runs_dependency_hooks(self, config):
        mock_gh = AsyncMock()
        with (
            _patch_session_exists(True),
            _patch_find_next(None),
            patch(f"{_LC}._check_dependencies", new_callable=AsyncMock) as mock_deps,
        ):
            await on_issue_closed(make_close_event(["feynman"]), config, mock_gh, db=AsyncMock())
        mock_deps.assert_awaited_once()

    async def test_repo_owner_gets_next_repo_local_issue(self, tmp_path):
        config = make_config(
            tmp_path,
            agents=AgentsConfig(
                specs={"backbone": AgentSpec(name="backbone", dir="/x", repo="acme/backbone")}
            ),
        )
        closed = IssueData(
            number=10,
            title="Done",
            state="closed",
            labels=ParsedLabels(),
            repo_full_name="acme/backbone",
        )
        event = IssueEvent(event_type=EventType.ISSUE_CLOSED, issue=closed)
        next_issue = IssueData(
            number=11, title="Next", labels=ParsedLabels(), repo_full_name="acme/backbone"
        )
        mock_gh = AsyncMock()
        mock_gh.list_issues = AsyncMock(return_value=[next_issue])
        mock_gh.list_open_issues = AsyncMock(return_value=[])

        with _patch_session_exists(True), _patch_deliver() as d:
            result = await on_issue_closed(event, config, mock_gh)

        assert result["backbone"] == "delivered_#11"
        assert d.await_args.kwargs["target_entity"] == "backbone"

    async def test_purges_queued_messages_on_close(self, config):
        mock_db = AsyncMock()
        mock_db.purge_pending_for_issue = AsyncMock(return_value=2)
        with _patch_session_exists(True), _patch_find_next(None):
            result = await on_issue_closed(
                make_close_event(["feynman"]), config, AsyncMock(), db=mock_db
            )
        mock_db.purge_pending_for_issue.assert_awaited_once_with(10)
        assert result["feynman"] == "queue_empty"
