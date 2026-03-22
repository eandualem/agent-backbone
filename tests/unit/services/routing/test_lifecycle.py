"""Tests for flows/lifecycle.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from agent_backbone.config import BackboneConfig
from agent_backbone.models import EventType, IssueData, IssueEvent, ParsedLabels
from agent_backbone.services.registry import EntityEntry, EntityInstance, EntityRegistry, RepoInfo
from agent_backbone.services.routing import (
    _ONBOARDING_TITLE_PREFIX,
    _check_onboarding_chain,
    find_next_issue,
    on_issue_closed,
)
from agent_backbone.services.routing import clear as clear_dedup


def make_close_event(targets: list[str], repo_full_name: str = "") -> IssueEvent:
    labels = ParsedLabels(sender="ike", targets=targets, issue_type="task")
    issue = IssueData(
        number=10,
        title="[task] Done",
        state="closed",
        labels=labels,
        repo_full_name=repo_full_name,
    )
    return IssueEvent(event_type=EventType.ISSUE_CLOSED, issue=issue)


class TestOnIssueClosed:
    def setup_method(self):
        clear_dedup()

    async def test_delivers_next_issue(self, config):
        event = make_close_event(["feynman"])
        next_issue = IssueData(
            number=11,
            title="[task] Next thing",
            labels=ParsedLabels(sender="leo", targets=["feynman"], issue_type="task"),
        )
        mock_gh = AsyncMock()

        with (
            patch(
                "agent_backbone.services.routing._lifecycle.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.routing._lifecycle.find_next_issue",
                new_callable=AsyncMock,
                return_value=next_issue,
            ),
            patch(
                "agent_backbone.services.routing._lifecycle.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
        ):
            result = await on_issue_closed(event, config, mock_gh)

        assert result["feynman"] == "delivered_#11"
        mock_deliver.assert_called_once()

    async def test_queue_empty(self, config):
        event = make_close_event(["feynman"])
        mock_gh = AsyncMock()

        with (
            patch(
                "agent_backbone.services.routing._lifecycle.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.routing._lifecycle.find_next_issue",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await on_issue_closed(event, config, mock_gh)

        assert result["feynman"] == "queue_empty"

    async def test_session_offline(self, config):
        event = make_close_event(["feynman"])
        mock_gh = AsyncMock()

        with patch(
            "agent_backbone.services.routing._lifecycle.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            result = await on_issue_closed(event, config, mock_gh)

        assert result["feynman"] == "offline"

    async def test_skips_elias(self, config):
        event = make_close_event(["elias"])
        mock_gh = AsyncMock()
        result = await on_issue_closed(event, config, mock_gh)
        assert result["elias"] == "skipped"

    async def test_blocking_issues_first(self, config):
        """Verify that find_next_issue returns blocking issues first."""
        event = make_close_event(["ike"])
        blocking_issue = IssueData(
            number=20,
            title="[bug] Blocking",
            labels=ParsedLabels(
                sender="ada", targets=["ike"], issue_type="bug", priority="blocking"
            ),
        )
        mock_gh = AsyncMock()

        with (
            patch(
                "agent_backbone.services.routing._lifecycle.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.routing._lifecycle.find_next_issue",
                new_callable=AsyncMock,
                return_value=blocking_issue,
            ),
            patch(
                "agent_backbone.services.routing._lifecycle.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ),
        ):
            result = await on_issue_closed(event, config, mock_gh)

        assert result["ike"] == "delivered_#20"

    async def test_concrete_role_instance_target_delivers_next_issue(self):
        event = make_close_event(["bell-wf"])
        next_issue = IssueData(
            number=21,
            title="[task] Next for Bell WF",
            labels=ParsedLabels(sender="leo", targets=["bell-wf"], issue_type="task"),
        )
        config = BackboneConfig(
            webhook_secret="test-secret",
            registry=EntityRegistry(
                entities={
                    "bell-wf": EntityEntry(
                        session="bell-wf",
                        home="~/ws/core/code/WF/bell",
                        groups=["orchestrators"],
                        figure="",
                        role="Org Orchestrator",
                        organization="WF",
                        entity_type="role-instance",
                    )
                },
                repos=[],
            ),
        )
        mock_gh = AsyncMock()

        with (
            patch(
                "agent_backbone.services.routing._lifecycle.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.routing._lifecycle.find_next_issue",
                new_callable=AsyncMock,
                return_value=next_issue,
            ),
            patch(
                "agent_backbone.services.routing._lifecycle.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
        ):
            result = await on_issue_closed(event, config, mock_gh)

        assert result["bell-wf"] == "delivered_#21"
        assert mock_deliver.await_count == 1

    async def test_abstract_role_target_has_no_session(self):
        event = make_close_event(["bell"])
        config = BackboneConfig(
            webhook_secret="test-secret",
            registry=EntityRegistry(
                entities={
                    "bell": EntityEntry(
                        session=None,
                        home="~/ws/core/code/WF/bell",
                        groups=["orchestrators"],
                        figure="",
                        role="Org Orchestrator",
                        entity_type="role",
                        instances={
                            "wf": EntityInstance(
                                home="~/ws/core/code/WF/bell",
                                session="bell-wf",
                                organization="WF",
                            ),
                        },
                    )
                },
                repos=[],
            ),
        )
        mock_gh = AsyncMock()

        result = await on_issue_closed(event, config, mock_gh)

        assert result["bell"] == "no_session"

    async def test_dedup_prevents_redelivery(self, config):
        """Closing two issues in a row shouldn't re-deliver the same next issue."""
        next_issue = IssueData(
            number=6,
            title="[task] Tmux theming",
            labels=ParsedLabels(sender="leo", targets=["feynman"], issue_type="task"),
        )
        mock_gh = AsyncMock()

        with (
            patch(
                "agent_backbone.services.routing._lifecycle.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.routing._lifecycle.find_next_issue",
                new_callable=AsyncMock,
                return_value=next_issue,
            ),
            patch(
                "agent_backbone.services.routing._lifecycle.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
        ):
            # First close: delivers #6
            event1 = make_close_event(["feynman"])
            result1 = await on_issue_closed(event1, config, mock_gh)
            assert result1["feynman"] == "delivered_#6"

            # Second close: #6 is still next, but should be deduped
            event2 = make_close_event(["feynman"])
            result2 = await on_issue_closed(event2, config, mock_gh)
            assert result2["feynman"] == "deduped_#6"

        # safe_deliver should only be called once (first delivery)
        assert mock_deliver.call_count == 1

    async def test_find_next_issue_excludes_closed_number(self):
        """find_next_issue should filter out the just-closed issue number."""
        closed_issue = IssueData(
            number=10,
            title="[task] Just closed",
            labels=ParsedLabels(sender="ike", targets=["feynman"], issue_type="task"),
        )
        real_next = IssueData(
            number=11,
            title="[task] Actually next",
            labels=ParsedLabels(sender="leo", targets=["feynman"], issue_type="task"),
        )

        mock_gh = AsyncMock()
        # GitHub API returns both (closed one still appears as open)
        mock_gh.list_open_issues.return_value = [closed_issue, real_next]

        config = BackboneConfig(webhook_secret="s")
        result = await find_next_issue(config, "feynman", mock_gh, exclude_number=10)

        assert result is not None
        assert result.number == 11

    async def test_jarvis_http_delivery_on_close(self, config):
        """Jarvis HTTP target skips session_exists and delivers via safe_deliver."""
        event = make_close_event(["jarvis"])
        next_issue = IssueData(
            number=15,
            title="[task] Next for Jarvis",
            labels=ParsedLabels(sender="ike", targets=["jarvis"], issue_type="task"),
        )
        mock_gh = AsyncMock()

        with (
            patch(
                "agent_backbone.services.routing._lifecycle.is_http_target",
                return_value=True,
            ),
            patch(
                "agent_backbone.services.routing._lifecycle.resolve_entity_session",
                new_callable=AsyncMock,
                return_value="jarvis",
            ),
            patch(
                "agent_backbone.services.routing._lifecycle.find_next_issue",
                new_callable=AsyncMock,
                return_value=next_issue,
            ),
            patch(
                "agent_backbone.services.routing._lifecycle.session_exists",
                new_callable=AsyncMock,
                return_value=False,
            ) as mock_session_exists,
            patch(
                "agent_backbone.services.routing._lifecycle.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
        ):
            result = await on_issue_closed(event, config, mock_gh)

        # session_exists should NOT be called for HTTP targets
        mock_session_exists.assert_not_called()
        assert result["jarvis"] == "delivered_#15"
        mock_deliver.assert_called_once()

    async def test_non_default_repo_uses_same_repo_and_skips_orchestration_hooks(self, config):
        event = make_close_event(["feynman"], repo_full_name="WF/agent-shell")
        next_issue = IssueData(
            number=11,
            title="[task] Next thing",
            labels=ParsedLabels(sender="leo", targets=["feynman"], issue_type="task"),
            repo_full_name="WF/agent-shell",
        )
        mock_gh = AsyncMock()

        with (
            patch(
                "agent_backbone.services.routing._lifecycle.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.routing._lifecycle.find_next_issue",
                new_callable=AsyncMock,
                return_value=next_issue,
            ) as mock_find_next_issue,
            patch(
                "agent_backbone.services.routing._lifecycle.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ),
            patch(
                "agent_backbone.services.routing._lifecycle._check_dependencies",
                new_callable=AsyncMock,
            ) as mock_check_dependencies,
            patch(
                "agent_backbone.services.routing._lifecycle._check_onboarding_chain",
                new_callable=AsyncMock,
            ) as mock_check_onboarding_chain,
        ):
            result = await on_issue_closed(event, config, mock_gh)

        assert result["feynman"] == "delivered_#11"
        assert mock_find_next_issue.await_args.kwargs["repo_full_name"] == "WF/agent-shell"
        mock_check_dependencies.assert_not_awaited()
        mock_check_onboarding_chain.assert_not_awaited()

    async def test_repo_local_issue_falls_back_to_repo_target(self):
        issue = IssueData(
            number=10,
            title="Done",
            state="closed",
            labels=ParsedLabels(sender="unknown", targets=[], issue_type="task"),
            repo_full_name="eandualem/agent-backbone",
        )
        event = IssueEvent(event_type=EventType.ISSUE_CLOSED, issue=issue)
        next_issue = IssueData(
            number=11,
            title="Next repo issue",
            labels=ParsedLabels(sender="unknown", targets=[], issue_type="task"),
            repo_full_name="eandualem/agent-backbone",
        )
        config = BackboneConfig(
            webhook_secret="test-secret",
            registry=EntityRegistry(
                entities={},
                repos=[RepoInfo(org="WF", name="agent-backbone", path="/some/path")],
            ),
        )
        mock_gh = AsyncMock()
        mock_gh.list_issues.return_value = [next_issue]

        with (
            patch(
                "agent_backbone.services.routing._lifecycle.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.routing._lifecycle.safe_deliver",
                new_callable=AsyncMock,
                return_value="delivered",
            ) as mock_deliver,
        ):
            result = await on_issue_closed(event, config, mock_gh)

        assert result["agent-backbone"] == "delivered_#11"
        assert mock_gh.list_issues.await_count >= 1
        assert mock_deliver.await_args.kwargs["target_entity"] == "agent-backbone"

    async def test_purges_queued_messages_on_close(self, config):
        """Closing an issue purges pending queue messages for that issue (#780)."""
        event = make_close_event(["feynman"])
        mock_gh = AsyncMock()
        mock_db = AsyncMock()
        mock_db.purge_pending_for_issue = AsyncMock(return_value=2)

        with (
            patch(
                "agent_backbone.services.routing._lifecycle.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.routing._lifecycle.find_next_issue",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await on_issue_closed(event, config, mock_gh, db=mock_db)

        mock_db.purge_pending_for_issue.assert_awaited_once_with("", 10)
        assert result["feynman"] == "queue_empty"


# ---------------------------------------------------------------------------
# Onboarding chain: Brunel closure → Leo notification
# ---------------------------------------------------------------------------


def _make_brunel_close_event(org: str, repo: str, number: int = 50) -> IssueEvent:
    """Create a close event matching the onboarding verification pattern."""
    title = f"{_ONBOARDING_TITLE_PREFIX}{org}/{repo}"
    labels = ParsedLabels(
        sender="coding-agent",
        targets=["brunel"],
        issue_type="task",
    )
    issue = IssueData(number=number, title=title, state="closed", labels=labels)
    return IssueEvent(event_type=EventType.ISSUE_CLOSED, issue=issue)


class TestOnboardingChain:
    async def test_creates_leo_issue_on_brunel_close(self):
        """When Brunel closes a verification issue, create_and_notify is called for Leo."""
        config = BackboneConfig()
        event = _make_brunel_close_event("WF", "new-thing", number=42)

        mock_gh = AsyncMock()

        with patch(
            "agent_backbone.services.routing._lifecycle.create_and_notify",
            new_callable=AsyncMock,
        ) as mock_create_notify:
            await _check_onboarding_chain(event, config, mock_gh)

        mock_create_notify.assert_called_once()
        call_kwargs = mock_create_notify.call_args.kwargs
        assert "for:leo" in call_kwargs["labels"]
        assert "from:backbone" in call_kwargs["labels"]
        assert "WF/new-thing" in call_kwargs["title"]
        assert "#42" in call_kwargs["body"]
        assert call_kwargs["config"] is config
        assert call_kwargs["flow_name"] == "issue-lifecycle"
        # gh is the first positional arg
        assert mock_create_notify.call_args.args[0] is mock_gh

    async def test_ignores_non_onboarding_issues(self):
        """Non-onboarding issues are silently ignored."""
        config = BackboneConfig()
        event = make_close_event(["brunel"])  # generic close, wrong title

        mock_gh = AsyncMock()

        with patch(
            "agent_backbone.services.routing._lifecycle.create_and_notify",
            new_callable=AsyncMock,
        ) as mock_create_notify:
            await _check_onboarding_chain(event, config, mock_gh)

        mock_create_notify.assert_not_called()

    async def test_ignores_non_brunel_targets(self):
        """Onboarding-titled issue for non-brunel targets is ignored."""
        config = BackboneConfig()
        title = f"{_ONBOARDING_TITLE_PREFIX}WF/some-repo"
        labels = ParsedLabels(
            sender="coding-agent",
            targets=["feynman"],
            issue_type="task",
        )
        issue = IssueData(
            number=99,
            title=title,
            state="closed",
            labels=labels,
        )
        event = IssueEvent(event_type=EventType.ISSUE_CLOSED, issue=issue)

        mock_gh = AsyncMock()

        with patch(
            "agent_backbone.services.routing._lifecycle.create_and_notify",
            new_callable=AsyncMock,
        ) as mock_create_notify:
            await _check_onboarding_chain(event, config, mock_gh)

        mock_create_notify.assert_not_called()

    async def test_no_auth_gate_for_onboarding_chain(self):
        """The lifecycle chain no longer skips based on PAT presence."""
        config = BackboneConfig()
        event = _make_brunel_close_event("WF", "new-thing")

        mock_gh = AsyncMock()

        with patch(
            "agent_backbone.services.routing._lifecycle.create_and_notify",
            new_callable=AsyncMock,
        ) as mock_create_notify:
            await _check_onboarding_chain(event, config, mock_gh)

        mock_create_notify.assert_called_once()

    async def test_error_does_not_block_lifecycle(self):
        """create_and_notify failure is logged, not raised."""
        config = BackboneConfig()
        event = _make_brunel_close_event("WF", "new-thing")

        mock_gh = AsyncMock()

        with patch(
            "agent_backbone.services.routing._lifecycle.create_and_notify",
            new_callable=AsyncMock,
            side_effect=RuntimeError("API down"),
        ):
            # Should not raise
            await _check_onboarding_chain(event, config, mock_gh)
