"""Tests for routing/_router.py — issue and comment dispatch."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import respx

from agent_backbone.config import AgentsConfig, AgentSpec
from agent_backbone.models import (
    CommentData,
    DeliveryOutcome,
    EventType,
    IssueData,
    IssueEvent,
    ParsedLabels,
    ReviewData,
    parse_from_tag,
)
from agent_backbone.services.github import API_BASE, GitHubClient
from agent_backbone.services.routing import list_open_queue_for_target
from agent_backbone.services.routing._router import issue_dispatcher
from agent_backbone.services.routing._targets import route_issue
from tests.conftest import TEST_REPO, make_config

OTHER_REPO = "acme/app"


class TestCompleteWorkQueue:
    async def test_swarm_member_only_receives_explicit_work(self, tmp_path):
        config = make_config(
            tmp_path,
            agents=AgentsConfig(
                specs={
                    "app": AgentSpec(name="app", dir="/app", repo=OTHER_REPO),
                    "scout": AgentSpec(
                        name="scout", dir="/swarm", repo=OTHER_REPO, tags=("swarm:audit",)
                    ),
                }
            ),
        )
        unlabelled = IssueData(number=1, title="Owner work", repo_full_name=OTHER_REPO)
        explicit = IssueData(
            number=2,
            title="Scout work",
            repo_full_name=OTHER_REPO,
            labels=ParsedLabels(targets=["scout"]),
        )
        gh = AsyncMock()

        async def listing(**kwargs):
            labels = kwargs.get("labels")
            return (
                [explicit]
                if labels == ["for:scout"]
                else ([] if labels else [unlabelled, explicit])
            )

        gh.list_issues.side_effect = listing
        assert [i.number for i in await list_open_queue_for_target(config, "scout", gh)] == [2]
        assert [i.number for i in await list_open_queue_for_target(config, "app", gh)] == [1]

    @pytest.mark.parametrize("targets", [[], ["app"]])
    async def test_self_created_issue_stays_suppressed_on_monitor_tick(self, tmp_path, db, targets):
        from agent_backbone.services.agents import AgentState, StateSnapshot
        from agent_backbone.services.jobs.pending import deliver_pending_issues

        config = make_config(
            tmp_path,
            agents=AgentsConfig(
                specs={
                    "app": AgentSpec(name="app", dir="/app", repo=OTHER_REPO),
                }
            ),
        )
        issue = IssueData(
            number=1,
            title="Own work",
            repo_full_name=OTHER_REPO,
            labels=ParsedLabels(sender="app", targets=targets),
        )
        gh = AsyncMock()
        gh.list_issues.return_value = [issue]
        with _patch_safe_deliver() as event_send:
            await issue_dispatcher(
                IssueEvent(event_type=EventType.ISSUE_OPENED, issue=issue), config, db, gh
            )
        event_send.assert_not_awaited()
        with patch(
            "agent_backbone.services.jobs.pending.safe_deliver", AsyncMock()
        ) as pending_send:
            result = await deliver_pending_issues(
                config, {"app": StateSnapshot(state=AgentState.IDLE)}, db, gh
            )
        assert result == {"app": "no_pending"}
        pending_send.assert_not_awaited()

    @respx.mock
    async def test_queue_sorts_blocking_issue_from_page_two(self, tmp_path):
        config = make_config(
            tmp_path,
            agents=AgentsConfig(
                specs={
                    "app": AgentSpec(name="app", dir="/app", watches=(OTHER_REPO,)),
                }
            ),
        )
        url = f"{API_BASE}/repos/{OTHER_REPO}/issues"
        second = f"{url}?page=2"
        respx.get(url__eq=second).respond(
            json=[
                {
                    "number": 51,
                    "title": "Urgent",
                    "labels": [{"name": "for:app"}, {"name": "blocking"}],
                }
            ]
        )
        respx.get(url__startswith=url).respond(
            json=[{"number": n, "labels": [{"name": "for:app"}]} for n in range(1, 51)],
            headers={"Link": f'<{second}>; rel="next"'},
        )
        async with GitHubClient(config) as gh:
            issues = await list_open_queue_for_target(config, "app", gh)
        assert len(issues) == 51
        assert issues[0].number == 51


def _patch_safe_deliver(outcome: DeliveryOutcome = DeliveryOutcome.DELIVERED):
    return patch(
        "agent_backbone.services.routing._router.safe_deliver",
        new_callable=AsyncMock,
        return_value=outcome,
    )


def _patch_find_outgoing(result: str | None):
    return patch(
        "agent_backbone.services.routing._router.find_outgoing_comment", return_value=result
    )


def _issue_event(number: int, sender: str, targets: list[str], **kwargs) -> IssueEvent:
    labels = ParsedLabels(sender=sender, targets=targets, issue_type="task", **kwargs)
    # A repository nobody owns or watches: routing comes from the labels alone.
    issue = IssueData(
        number=number, repo_full_name=OTHER_REPO, title=f"[task] #{number}", labels=labels
    )
    return IssueEvent(event_type=EventType.ISSUE_OPENED, issue=issue)


def _comment_event(number: int, sender: str, targets: list[str], body: str) -> IssueEvent:
    labels = ParsedLabels(sender=sender, targets=targets, issue_type="task")
    issue = IssueData(
        number=number, repo_full_name=OTHER_REPO, title=f"[task] #{number}", labels=labels
    )
    comment = CommentData(body=body, user_login="someone")
    return IssueEvent(event_type=EventType.COMMENT_CREATED, issue=issue, comment=comment)


def _review_event(
    number: int, sender: str, targets: list[str], body: str, state: str = "commented"
) -> IssueEvent:
    labels = ParsedLabels(sender=sender, targets=targets, issue_type="task")
    issue = IssueData(
        number=number, repo_full_name=OTHER_REPO, title=f"feat: #{number}", labels=labels
    )
    review = ReviewData(
        id=9, body=body, user_login="coderabbitai[bot]", state=state, html_url="https://r/9"
    )
    return IssueEvent(event_type=EventType.REVIEW_SUBMITTED, issue=issue, review=review)


class TestReviewDispatch:
    async def test_review_reaches_the_pull_requests_parties(self, config, mock_db):
        event = _review_event(41, "ike", ["feynman"], "Two findings.", state="changes_requested")
        with _patch_safe_deliver() as deliver:
            result = await issue_dispatcher(event, config, mock_db)
        assert sorted(result.delivered) == ["feynman", "ike"]
        message = deliver.await_args.args[1]
        assert message.startswith("[via:github pr:41] Review on acme/app#41")
        assert "from coderabbitai[bot] (changes requested)" in message
        assert '"Two findings."' in message and "https://r/9" in message
        # A queued copy is identified by the review, so a retry never stores it twice.
        assert deliver.await_args.kwargs["source_key"] == "review:acme/app#41:9"
        assert deliver.await_args.kwargs["sender"] == "coderabbitai[bot]"

    async def test_an_agent_reviewer_is_not_told_about_its_own_review(self, config, mock_db):
        event = _review_event(41, "ike", ["feynman"], "[from:feynman] LGTM", state="approved")
        with _patch_safe_deliver() as deliver:
            result = await issue_dispatcher(event, config, mock_db)
        assert result.delivered == ["ike"]
        assert "from feynman (approved)" in deliver.await_args.args[1]

    async def test_empty_review_body_points_at_the_inline_comments(self, config, mock_db):
        event = _review_event(41, "ike", ["feynman"], "")
        with _patch_safe_deliver() as deliver:
            await issue_dispatcher(event, config, mock_db)
        assert "(no summary; see the inline comments)" in deliver.await_args.args[1]


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.acks.record = AsyncMock()
    db.acks.clear = AsyncMock()
    db.deliveries.record = AsyncMock()
    return db


class TestParseFromTag:
    def test_basic_tag(self):
        assert parse_from_tag("[from:ike]") == "ike"

    def test_hyphenated_entity(self):
        assert parse_from_tag("[from:agent-backbone]") == "agent-backbone"

    def test_case_insensitive(self):
        assert parse_from_tag("[from:IKE]") == "ike"

    def test_no_tag_returns_none(self):
        assert parse_from_tag("just a regular comment") is None

    def test_tag_not_at_start(self):
        assert parse_from_tag("some text [from:ike]") is None

    def test_empty_string(self):
        assert parse_from_tag("") is None

    def test_leading_whitespace_stripped(self):
        assert parse_from_tag("  [from:ike]") == "ike"

    def test_names_use_the_registration_vocabulary(self):
        # sanitize_name allows digits first, underscores and dots: such an
        # agent must be able to acknowledge.
        assert parse_from_tag("[from:123-agent]") == "123-agent"
        assert parse_from_tag("[from:app_test.v2]") == "app_test.v2"


class TestIssueDispatcher:
    async def test_dedup_is_per_target(self, config, mock_db):
        """A target announced moments ago is skipped; the others still get the issue."""
        from agent_backbone.services.routing._dedup import is_recent_notification

        is_recent_notification(OTHER_REPO, 5, "ike")  # records ike as just-notified
        with _patch_safe_deliver(DeliveryOutcome.DELIVERED) as mock_deliver:
            result = await issue_dispatcher(
                _issue_event(5, "leo", ["ike", "feynman"]), config, mock_db
            )
        assert result.delivered == ["feynman"]
        assert "ike" in result.skipped
        assert [c.kwargs["target_entity"] for c in mock_deliver.await_args_list] == ["feynman"]

    async def test_dispatch_to_configured_agent(self, config, mock_db):
        with _patch_safe_deliver(DeliveryOutcome.DELIVERED) as mock_deliver:
            result = await issue_dispatcher(_issue_event(1, "leo", ["ike"]), config, mock_db)

        assert result.delivered == ["ike"]
        assert mock_deliver.await_args.args[0] == "ike"
        assert mock_deliver.await_args.kwargs["target_entity"] == "ike"

    async def test_ignored_target_is_skipped(self, config, mock_db):
        result = await issue_dispatcher(_issue_event(2, "ike", ["elias"]), config, mock_db)
        assert "elias" in result.skipped
        assert result.delivered == []

    async def test_unknown_target_is_skipped(self, config, mock_db):
        with _patch_safe_deliver(DeliveryOutcome.DELIVERED) as mock_deliver:
            result = await issue_dispatcher(_issue_event(3, "leo", ["nobody"]), config, mock_db)

        assert result.skipped == ["nobody"]
        mock_deliver.assert_not_called()

    async def test_session_offline(self, config, mock_db):
        with _patch_safe_deliver(DeliveryOutcome.DELIVERY_FAILED):
            result = await issue_dispatcher(_issue_event(3, "leo", ["feynman"]), config, mock_db)
        assert "feynman" in result.offline

    async def test_multiple_targets(self, config, mock_db):
        with _patch_safe_deliver(DeliveryOutcome.DELIVERED):
            result = await issue_dispatcher(
                _issue_event(5, "leo", ["ike", "feynman"]), config, mock_db
            )
        assert sorted(result.delivered) == ["feynman", "ike"]

    async def test_ignores_unknown_event(self, config, mock_db):
        issue = IssueData(
            number=6,
            repo_full_name=TEST_REPO,
            title="Whatever",
            labels=ParsedLabels(targets=["ike"]),
        )
        event = IssueEvent(event_type=EventType.UNKNOWN, issue=issue)
        result = await issue_dispatcher(event, config, mock_db)
        assert result.delivered == [] and result.offline == []

    async def test_defers_busy_agent(self, config, mock_db):
        with _patch_safe_deliver(DeliveryOutcome.AGENT_WORKING):
            result = await issue_dispatcher(_issue_event(7, "leo", ["ike"]), config, mock_db)
        assert "ike" in result.deferred

    async def test_blocking_sets_priority(self, config, mock_db):
        event = _issue_event(8, "leo", ["ike"], priority="blocking")
        with _patch_safe_deliver(DeliveryOutcome.DELIVERED) as mock_deliver:
            await issue_dispatcher(event, config, mock_db)
        assert mock_deliver.await_args.kwargs["priority"] is True

    async def test_self_targeted_issue_is_suppressed(self, config, mock_db):
        with _patch_safe_deliver(DeliveryOutcome.DELIVERED) as mock_deliver:
            result = await issue_dispatcher(_issue_event(9, "ike", ["ike"]), config, mock_db)
        assert result.skipped == ["ike"]
        mock_deliver.assert_not_called()

    async def test_queue_scope_loaded_from_github(self, config, mock_db):
        mock_gh = AsyncMock()
        mock_gh.list_issues = AsyncMock(
            return_value=[
                IssueData(number=1, repo_full_name=TEST_REPO),
                IssueData(number=4, repo_full_name=TEST_REPO),
            ]
        )
        with _patch_safe_deliver(DeliveryOutcome.DELIVERED) as mock_deliver:
            await issue_dispatcher(_issue_event(1, "leo", ["ike"]), config, mock_db, mock_gh)
        assert mock_deliver.await_args.kwargs["queue_scope"] == {(TEST_REPO, 1), (TEST_REPO, 4)}
        assert mock_deliver.await_args.kwargs["enforce_issue_queue"] is True

    async def test_a_pull_request_is_not_announced_to_the_agent_that_opened_it(
        self, tmp_path, mock_db
    ):
        config = make_config(
            tmp_path,
            agents=AgentsConfig(
                specs={
                    "backbone": AgentSpec(name="backbone", dir="/x", repo="acme/backbone"),
                    "leo": AgentSpec(name="leo", dir="/y", watches=("acme/backbone",)),
                }
            ),
        )
        issue = IssueData(
            number=7,
            repo_full_name="acme/backbone",
            title="feat: x",
            body="Does things.\n\nCloses #3 and fixes #5.",
            head_ref="feat/x",
            head_repo="acme/backbone",
        )
        event = IssueEvent(event_type=EventType.PULL_REQUEST_OPENED, issue=issue)
        with (
            _patch_safe_deliver() as deliver,
            patch(
                "agent_backbone.services.routing._router.find_outgoing_pull_request",
                return_value="backbone",
            ) as opener,
        ):
            result = await issue_dispatcher(event, config, mock_db)
        assert opener.call_args.args[:2] == ("acme/backbone", "feat/x")  # head repo, branch
        assert opener.call_args.kwargs["base_repo"] == "acme/backbone"
        assert result.delivered == ["leo"] and result.skipped == ["backbone"]
        assert deliver.await_count == 1
        recorded = sorted(call.args[:2] for call in mock_db.acks.record.await_args_list)
        assert recorded == [(3, "backbone"), (5, "backbone")]

    async def test_a_fork_pull_request_is_matched_on_its_head_repository(self, tmp_path, mock_db):
        config = make_config(
            tmp_path,
            agents=AgentsConfig(
                specs={"backbone": AgentSpec(name="backbone", dir="/x", repo="acme/backbone")}
            ),
        )
        issue = IssueData(
            number=9, repo_full_name="acme/backbone", head_ref="feat/x", head_repo="forker/backbone"
        )
        event = IssueEvent(event_type=EventType.PULL_REQUEST_OPENED, issue=issue)
        with (
            _patch_safe_deliver(),
            patch(
                "agent_backbone.services.routing._router.find_outgoing_pull_request",
                return_value=None,
            ) as opener,
        ):
            result = await issue_dispatcher(event, config, mock_db)
        assert opener.call_args.args[:2] == ("forker/backbone", "feat/x")
        assert result.delivered == ["backbone"]

    async def test_a_pull_request_nobody_logged_reaches_everyone(self, tmp_path, mock_db):
        config = make_config(
            tmp_path,
            agents=AgentsConfig(
                specs={"backbone": AgentSpec(name="backbone", dir="/x", repo="acme/backbone")}
            ),
        )
        issue = IssueData(number=8, repo_full_name="acme/backbone", head_ref="feat/z")
        event = IssueEvent(event_type=EventType.PULL_REQUEST_OPENED, issue=issue)
        with (
            _patch_safe_deliver(),
            patch(
                "agent_backbone.services.routing._router.find_outgoing_pull_request",
                return_value=None,
            ),
        ):
            result = await issue_dispatcher(event, config, mock_db)
        assert result.delivered == ["backbone"]
        mock_db.acks.record.assert_not_called()

    async def test_repo_owner_receives_repo_local_issue(self, tmp_path, mock_db):
        config = make_config(
            tmp_path,
            agents=AgentsConfig(
                specs={"backbone": AgentSpec(name="backbone", dir="/x", repo="acme/backbone")}
            ),
        )
        issue = IssueData(
            number=77,
            title="Fix webhook fallback",
            labels=ParsedLabels(),
            repo_full_name="acme/backbone",
        )
        event = IssueEvent(event_type=EventType.ISSUE_OPENED, issue=issue)
        mock_gh = AsyncMock()
        mock_gh.list_issues = AsyncMock(return_value=[issue])

        with _patch_safe_deliver(DeliveryOutcome.DELIVERED) as mock_deliver:
            result = await issue_dispatcher(event, config, mock_db, mock_gh)

        assert result.delivered == ["backbone"]
        mock_deliver.assert_awaited_once()
        assert mock_gh.list_issues.await_args.kwargs["repo_full_name"] == "acme/backbone"

    async def test_pull_request_routes_to_repo_owner_without_queue_gate(self, tmp_path, mock_db):
        config = make_config(
            tmp_path,
            agents=AgentsConfig(
                specs={"backbone": AgentSpec(name="backbone", dir="/x", repo="acme/backbone")}
            ),
        )
        issue = IssueData(
            number=78,
            title="Add PR notification path",
            labels=ParsedLabels(),
            html_url="https://github.com/acme/backbone/pull/78",
            repo_full_name="acme/backbone",
        )
        event = IssueEvent(event_type=EventType.PULL_REQUEST_OPENED, issue=issue)

        with _patch_safe_deliver(DeliveryOutcome.DELIVERED) as mock_deliver:
            result = await issue_dispatcher(event, config, mock_db, AsyncMock())

        assert result.delivered == ["backbone"]
        assert mock_deliver.await_args.kwargs["delivery_kind"] == "pull_request"
        assert mock_deliver.await_args.kwargs["enforce_issue_queue"] is False

    async def test_watchers_are_informed_but_not_queued(self, tmp_path, mock_db):
        config = make_config(
            tmp_path,
            agents=AgentsConfig(
                specs={
                    "app": AgentSpec(name="app", dir="/x", repo="acme/app"),
                    "orch": AgentSpec(
                        name="orch", dir="/o", repo="acme/orch", watches=("acme/app",)
                    ),
                }
            ),
        )
        issue = IssueData(
            number=3, title="Bug", labels=ParsedLabels(sender="leo"), repo_full_name="acme/app"
        )
        event = IssueEvent(event_type=EventType.ISSUE_OPENED, issue=issue)
        with _patch_safe_deliver(DeliveryOutcome.DELIVERED) as mock_deliver:
            result = await issue_dispatcher(event, config, mock_db)
        kinds = {c.args[0]: c.kwargs["delivery_kind"] for c in mock_deliver.await_args_list}
        assert kinds == {"app": "issue", "orch": "watch"}
        assert sorted(result.delivered) == ["app", "orch"]

    async def test_multi_owner_repo_announces_unassigned_issue(self, tmp_path, mock_db):
        config = make_config(
            tmp_path,
            agents=AgentsConfig(
                specs={
                    "a": AgentSpec(name="a", dir="/a", repo="acme/app"),
                    "b": AgentSpec(name="b", dir="/b", repo="acme/app"),
                }
            ),
        )
        issue = IssueData(
            number=3, title="Bug", labels=ParsedLabels(sender="leo"), repo_full_name="acme/app"
        )
        event = IssueEvent(event_type=EventType.ISSUE_OPENED, issue=issue)
        with _patch_safe_deliver(DeliveryOutcome.DELIVERED) as mock_deliver:
            await issue_dispatcher(event, config, mock_db)
        assert {c.kwargs["delivery_kind"] for c in mock_deliver.await_args_list} == {"watch"}
        assert "comment on it to claim it" in mock_deliver.await_args.args[1]

    async def test_labeled_event_without_for_is_ignored(self, config, mock_db):
        issue = IssueData(
            number=3, title="Edit", labels=ParsedLabels(sender="leo"), repo_full_name="example/ike"
        )
        event = IssueEvent(event_type=EventType.ISSUE_LABELED, issue=issue)
        with _patch_safe_deliver(DeliveryOutcome.DELIVERED) as mock_deliver:
            await issue_dispatcher(event, config, mock_db)
        mock_deliver.assert_not_called()


class TestCommentRouting:
    async def test_unknown_commenter_notifies_everyone(self, config, mock_db):
        event = _comment_event(4, "leo", ["ike"], "Test comment")
        with _patch_safe_deliver(DeliveryOutcome.DELIVERED), _patch_find_outgoing(None):
            result = await issue_dispatcher(event, config, mock_db)
        assert sorted(result.delivered) == ["ike", "leo"]

    async def test_from_tag_suppresses_self_notification(self, config, mock_db):
        event = _comment_event(9, "leo", ["ike"], "[from:ike] Done with this.")
        with _patch_safe_deliver(DeliveryOutcome.DELIVERED):
            result = await issue_dispatcher(event, config, mock_db)
        assert result.delivered == ["leo"]

    async def test_self_comment_records_acknowledgment(self, config, mock_db):
        event = _comment_event(42, "leo", ["ike"], "[from:ike] Ack")
        with _patch_safe_deliver(DeliveryOutcome.DELIVERED):
            await issue_dispatcher(event, config, mock_db)
        mock_db.acks.record.assert_called_once_with(42, "ike", repo=OTHER_REPO)

    async def test_external_comment_clears_acknowledgment(self, config, mock_db):
        event = _comment_event(42, "leo", ["ike"], "[from:leo] New info")
        with _patch_safe_deliver(DeliveryOutcome.DELIVERED):
            result = await issue_dispatcher(event, config, mock_db)
        assert result.delivered == ["ike"]
        mock_db.acks.clear.assert_called_with(42, "ike", repo=OTHER_REPO)

    async def test_no_from_tag_falls_back_to_action_log(self, config, mock_db):
        event = _comment_event(12, "leo", ["ike"], "Just a plain comment.")
        with _patch_safe_deliver(DeliveryOutcome.DELIVERED), _patch_find_outgoing("ike"):
            result = await issue_dispatcher(event, config, mock_db)
        assert result.delivered == ["leo"]

    async def test_comment_delivery_kind(self, config, mock_db):
        event = _comment_event(13, "leo", ["ike"], "[from:leo] hi")
        with _patch_safe_deliver(DeliveryOutcome.DELIVERED) as mock_deliver:
            await issue_dispatcher(event, config, mock_db)
        assert mock_deliver.await_args.kwargs["delivery_kind"] == "comment"

    async def test_ignored_target_in_comment_routing(self, config, mock_db):
        event = _comment_event(15, "ike", ["elias"], "[from:leo] Thoughts?")
        with _patch_safe_deliver(DeliveryOutcome.DELIVERED):
            result = await issue_dispatcher(event, config, mock_db)
        assert "elias" not in result.delivered
        assert result.delivered == ["ike"]

    async def test_multiple_targets_comment(self, config, mock_db):
        event = _comment_event(16, "leo", ["ike", "feynman"], "[from:feynman] I'll handle this.")
        with _patch_safe_deliver(DeliveryOutcome.DELIVERED):
            result = await issue_dispatcher(event, config, mock_db)
        assert sorted(result.delivered) == ["ike", "leo"]

    async def test_repo_owner_fallback_for_untargeted_comment(self, tmp_path, mock_db):
        config = make_config(
            tmp_path,
            agents=AgentsConfig(
                specs={"backbone": AgentSpec(name="backbone", dir="/x", repo="acme/backbone")}
            ),
        )
        issue = IssueData(
            number=5, title="x", labels=ParsedLabels(), repo_full_name="acme/backbone"
        )
        event = IssueEvent(
            event_type=EventType.COMMENT_CREATED,
            issue=issue,
            comment=CommentData(body="hello", user_login="someone"),
        )
        with _patch_safe_deliver(DeliveryOutcome.DELIVERED), _patch_find_outgoing(None):
            result = await issue_dispatcher(event, config, mock_db)
        assert result.delivered == ["backbone"]


class TestAddressedIssues:
    def test_issue_addressed_to_someone_the_backbone_does_not_route_is_not_the_owners(
        self, tmp_path
    ):
        # S1-5: `for:nobody` (a person, or an unknown name) is still addressed;
        # the sole owner must not receive it as if it were unlabelled.
        config = make_config(
            tmp_path,
            agents=AgentsConfig(
                specs={"backbone": AgentSpec(name="backbone", dir="/x", repo="acme/backbone")}
            ),
        )
        addressed = IssueData(
            number=1,
            title="t",
            labels=ParsedLabels(targets=["nobody"]),
            repo_full_name="acme/backbone",
        )
        unlabelled = IssueData(
            number=2, title="t", labels=ParsedLabels(), repo_full_name="acme/backbone"
        )
        assert route_issue(addressed, EventType.ISSUE_OPENED, config).queue == []
        assert route_issue(unlabelled, EventType.ISSUE_OPENED, config).queue == ["backbone"]


class TestSwarmCoordinatorIsAParty:
    async def test_comment_on_the_swarm_issue_reaches_the_coordinator(self, tmp_path, mock_db):
        # Members are not owners (#137); the coordinator still hears its own issue.
        config = make_config(
            tmp_path,
            agents=AgentsConfig(
                specs={
                    "backbone": AgentSpec(name="backbone", dir="/x", repo="acme/backbone"),
                    "audit-coordinator": AgentSpec(
                        name="audit-coordinator",
                        dir="/x/.backbone/swarms/audit",
                        repo="acme/backbone",
                        tags=("swarm:audit", "role:coordinator"),
                    ),
                }
            ),
        )
        issue = IssueData(
            number=133, title="Audit", labels=ParsedLabels(), repo_full_name="acme/backbone"
        )
        event = IssueEvent(
            event_type=EventType.COMMENT_CREATED,
            issue=issue,
            comment=CommentData(id=1, body="how is it going?", user_login="elias"),
        )
        mock_db.swarms.active_for_issue = AsyncMock(
            return_value={"name": "audit", "coordinator": "audit-coordinator"}
        )
        with _patch_safe_deliver(DeliveryOutcome.DELIVERED):
            result = await issue_dispatcher(event, config, mock_db)
        assert "audit-coordinator" in result.delivered
        assert "backbone" in result.delivered  # the sole owner is a party too

    async def test_no_swarm_means_no_extra_party(self, tmp_path, mock_db):
        config = make_config(
            tmp_path,
            agents=AgentsConfig(
                specs={"backbone": AgentSpec(name="backbone", dir="/x", repo="acme/backbone")}
            ),
        )
        issue = IssueData(
            number=5, title="t", labels=ParsedLabels(), repo_full_name="acme/backbone"
        )
        event = IssueEvent(
            event_type=EventType.COMMENT_CREATED,
            issue=issue,
            comment=CommentData(id=2, body="hi", user_login="elias"),
        )
        mock_db.swarms.active_for_issue = AsyncMock(return_value=None)
        with _patch_safe_deliver(DeliveryOutcome.DELIVERED):
            result = await issue_dispatcher(event, config, mock_db)
        assert result.delivered == ["backbone"]
