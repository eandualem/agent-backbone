"""Tests for flows/issue_dispatcher.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.config import BackboneConfig
from agent_backbone.models import (
    CommentData,
    EventType,
    IssueData,
    IssueEvent,
    ParsedLabels,
    parse_from_tag,
    parse_governance_tag,
)
from agent_backbone.services.registry import EntityEntry, EntityInstance, EntityRegistry, RepoInfo
from agent_backbone.services.routing import clear as clear_dedup
from agent_backbone.services.routing import issue_dispatcher, resolve_session


def _patch_resolve():
    """Patch resolve_entity_session -- named entities map via config, coding-agent falls back."""

    async def _resolver(target, config, issue_title="", **kwargs):
        if target in config.entities.skip:
            return None
        if target in config.registry.sessions_map:
            return config.registry.sessions_map[target]
        return config.entities.fallback.get(target)

    return patch(
        "agent_backbone.services.routing._router.resolve_entity_session",
        side_effect=_resolver,
    )


def _patch_resolve_sessions(sessions: list[str]):
    """Patch resolve_entity_sessions to return a fixed session list."""
    return patch(
        "agent_backbone.services.routing._router.resolve_entity_sessions",
        new_callable=AsyncMock,
        return_value=sessions,
    )


def _patch_safe_deliver(outcome: str = "delivered"):
    """Patch safe_deliver to return a fixed outcome string."""
    return patch(
        "agent_backbone.services.routing._router.safe_deliver",
        new_callable=AsyncMock,
        return_value=outcome,
    )


def _patch_find_outgoing(result: str | None):
    """Patch find_outgoing_comment to return a fixed value."""
    return patch(
        "agent_backbone.services.routing._router.find_outgoing_comment",
        return_value=result,
    )


@pytest.fixture
def mock_db():
    """Mock BackboneDB for use as parameter."""
    db = AsyncMock()
    db.record_acknowledgment = AsyncMock()
    db.clear_acknowledgment = AsyncMock()
    db.record_delivery = AsyncMock()
    return db


@pytest.fixture(autouse=True)
def _clear_recent_notification_dedup():
    clear_dedup()
    yield
    clear_dedup()


# ---------------------------------------------------------------------------
# TestParseFromTag
# ---------------------------------------------------------------------------


class TestParseFromTag:
    """Tests for the parse_from_tag() function in agent_backbone/models.py."""

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

    def test_invalid_name_starts_with_digit(self):
        assert parse_from_tag("[from:123]") is None


# ---------------------------------------------------------------------------
# TestParseGovernanceTag
# ---------------------------------------------------------------------------


class TestParseGovernanceTag:
    """Tests for the parse_governance_tag() function in agent_backbone/models.py."""

    def test_extracts_event_type(self):
        assert parse_governance_tag("[governance:proof.submitted]") == "proof.submitted"

    def test_extracts_with_underscores(self):
        assert parse_governance_tag("[governance:step_completed]") == "step_completed"

    def test_found_anywhere_in_body(self):
        assert parse_governance_tag("Some text [governance:proof.submitted] more") == "proof.submitted"

    def test_no_tag_returns_none(self):
        assert parse_governance_tag("regular comment") is None

    def test_empty_string_returns_none(self):
        assert parse_governance_tag("") is None


# ---------------------------------------------------------------------------
# TestResolveSession
# ---------------------------------------------------------------------------


class TestResolveSession:
    async def test_delegates_to_resolve_entity_session(self, config):
        """resolve_session delegates to resolve_entity_session from session_bridge."""
        with patch(
            "agent_backbone.services.routing._router.resolve_entity_session",
            new_callable=AsyncMock,
            return_value="ike",
        ) as mock:
            result = await resolve_session("ike", "[task] Something", config)
        assert result == "ike"
        mock.assert_called_once()
        args = mock.call_args[0]
        assert args[0] == "ike"
        assert args[2] == "[task] Something"

    async def test_returns_none_when_bridge_returns_none(self, config):
        """resolve_session returns None when bridge cannot resolve."""
        with patch(
            "agent_backbone.services.routing._router.resolve_entity_session",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await resolve_session("nobody", "irrelevant", config)
        assert result is None


# ---------------------------------------------------------------------------
# TestIssueDispatcher — non-comment issue event paths
# ---------------------------------------------------------------------------


class TestIssueDispatcher:
    async def test_dispatch_to_named_entity(self, config, mock_db):
        labels = ParsedLabels(sender="leo", targets=["ike"], issue_type="task")
        issue = IssueData(number=1, title="[task] Do thing", labels=labels)
        event = IssueEvent(event_type=EventType.ISSUE_OPENED, issue=issue)

        with (
            _patch_resolve(),
            _patch_safe_deliver("delivered") as mock_deliver,
        ):
            result = await issue_dispatcher(event, config, mock_db)

        assert "ike" in result.delivered
        assert mock_deliver.called

    async def test_skip_elias(self, config, mock_db):
        labels = ParsedLabels(sender="ike", targets=["elias"], issue_type="question")
        issue = IssueData(number=2, title="[question] Clarify", labels=labels)
        event = IssueEvent(event_type=EventType.ISSUE_OPENED, issue=issue)

        result = await issue_dispatcher(event, config, mock_db)
        assert "elias" in result.skipped
        assert result.delivered == []

    async def test_session_offline(self, config, mock_db):
        labels = ParsedLabels(sender="leo", targets=["feynman"], issue_type="task")
        issue = IssueData(number=3, title="[task] Something", labels=labels)
        event = IssueEvent(event_type=EventType.ISSUE_OPENED, issue=issue)

        with (
            _patch_resolve(),
            _patch_safe_deliver("delivery_failed"),
        ):
            result = await issue_dispatcher(event, config, mock_db)

        assert "feynman" in result.offline

    async def test_multiple_targets(self, config, mock_db):
        labels = ParsedLabels(sender="leo", targets=["ike", "feynman"], issue_type="task")
        issue = IssueData(number=5, title="[task] Both", labels=labels)
        event = IssueEvent(event_type=EventType.ISSUE_OPENED, issue=issue)

        with (
            _patch_resolve(),
            _patch_safe_deliver("delivered"),
        ):
            result = await issue_dispatcher(event, config, mock_db)

        assert len(result.delivered) == 2

    async def test_concrete_role_instance_target_delivers(self, config, mock_db):
        labels = ParsedLabels(sender="leo", targets=["bell-wf"], issue_type="task")
        issue = IssueData(number=15, title="[task] WF role instance", labels=labels)
        event = IssueEvent(event_type=EventType.ISSUE_OPENED, issue=issue)

        with (
            _patch_resolve_sessions(["bell-wf"]),
            _patch_safe_deliver("delivered") as mock_deliver,
        ):
            result = await issue_dispatcher(event, config, mock_db)

        assert result.delivered == ["bell-wf"]
        assert mock_deliver.await_count == 1

    async def test_abstract_role_target_is_skipped(self, mock_db):
        config = BackboneConfig(
            webhook_secret="test-secret",
            registry=EntityRegistry(
                entities={
                    "bell": EntityEntry(
                        session="bell",
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
        labels = ParsedLabels(sender="leo", targets=["bell"], issue_type="task")
        issue = IssueData(number=16, title="[task] Legacy shared role", labels=labels)
        event = IssueEvent(event_type=EventType.ISSUE_OPENED, issue=issue)

        with _patch_safe_deliver("delivered") as mock_deliver:
            result = await issue_dispatcher(event, config, mock_db)

        assert result.delivered == []
        assert result.skipped == ["bell"]
        assert mock_deliver.await_count == 0

    async def test_ignores_unknown_event(self, config, mock_db):
        labels = ParsedLabels(sender="leo", targets=["ike"])
        issue = IssueData(number=6, title="Whatever", labels=labels)
        event = IssueEvent(event_type=EventType.UNKNOWN, issue=issue)

        result = await issue_dispatcher(event, config, mock_db)
        assert result.delivered == []
        assert result.offline == []

    async def test_defers_busy_agent(self, config, mock_db):
        """Busy agents should get deferred, not delivered."""
        labels = ParsedLabels(sender="leo", targets=["ike"], issue_type="task")
        issue = IssueData(number=7, title="[task] Deferred", labels=labels)
        event = IssueEvent(event_type=EventType.ISSUE_OPENED, issue=issue)

        with (
            _patch_resolve(),
            _patch_safe_deliver("agent_working"),
        ):
            result = await issue_dispatcher(event, config, mock_db)

        assert "ike" in result.deferred
        assert result.delivered == []

    async def test_blocking_overrides_processing(self, config, mock_db):
        """Blocking issues should deliver even to processing agents."""
        labels = ParsedLabels(
            sender="leo",
            targets=["ike"],
            issue_type="bug",
            priority="blocking",
        )
        issue = IssueData(number=8, title="[bug] Critical", labels=labels)
        event = IssueEvent(event_type=EventType.ISSUE_OPENED, issue=issue)

        with (
            _patch_resolve(),
            _patch_safe_deliver("delivered"),
        ):
            result = await issue_dispatcher(event, config, mock_db)

        assert "ike" in result.delivered

    async def test_repo_local_issue_without_targets_routes_to_repo_session(self, mock_db):
        registry = EntityRegistry(
            entities={},
            repos=[RepoInfo(org="WF", name="agent-backbone", path="/some/path")],
        )
        config = BackboneConfig(webhook_secret="test-secret", registry=registry)
        labels = ParsedLabels(sender="unknown", targets=[], issue_type="")
        issue = IssueData(
            number=77,
            title="Fix webhook fallback",
            labels=labels,
            repo_full_name="eandualem/agent-backbone",
        )
        event = IssueEvent(event_type=EventType.ISSUE_OPENED, issue=issue)
        mock_gh = AsyncMock()
        mock_gh.list_issues.return_value = [issue]

        with (
            _patch_resolve_sessions(["agent-backbone"]),
            _patch_safe_deliver("delivered") as mock_deliver,
        ):
            result = await issue_dispatcher(event, config, mock_db, mock_gh)

        assert result.delivered == ["agent-backbone"]
        mock_deliver.assert_awaited_once()
        assert mock_gh.list_issues.await_args.kwargs["repo_full_name"] == "eandualem/agent-backbone"

    async def test_pull_request_routes_to_repo_session_without_queue_gate(self, mock_db):
        registry = EntityRegistry(
            entities={},
            repos=[RepoInfo(org="WF", name="agent-backbone", path="/some/path")],
        )
        config = BackboneConfig(webhook_secret="test-secret", registry=registry)
        issue = IssueData(
            number=78,
            title="Add PR notification path",
            labels=ParsedLabels(),
            html_url="https://github.com/eandualem/agent-backbone/pull/78",
            repo_full_name="eandualem/agent-backbone",
            is_pull_request=True,
        )
        event = IssueEvent(event_type=EventType.PULL_REQUEST_OPENED, issue=issue)
        mock_gh = AsyncMock()

        with (
            _patch_resolve_sessions(["agent-backbone"]),
            _patch_safe_deliver("delivered") as mock_deliver,
        ):
            result = await issue_dispatcher(event, config, mock_db, mock_gh)

        assert result.delivered == ["agent-backbone"]
        assert mock_deliver.await_args.kwargs["delivery_kind"] == "pull_request"
        assert mock_deliver.await_args.kwargs["enforce_issue_queue"] is False

    async def test_comment_event_unknown_commenter(self, config, mock_db):
        """When the commenter is unknown (no from-tag, no JSONL), both sender
        and target get notified."""
        labels = ParsedLabels(sender="leo", targets=["ike"], issue_type="task")
        issue = IssueData(number=4, title="[task] Something", labels=labels)
        comment = CommentData(body="Test comment", user_login="eandualem")
        event = IssueEvent(event_type=EventType.COMMENT_CREATED, issue=issue, comment=comment)

        with (
            _patch_resolve(),
            _patch_safe_deliver("delivered"),
            _patch_find_outgoing(None),
        ):
            result = await issue_dispatcher(event, config, mock_db)

        # Unknown commenter means nobody is subtracted from {sender} | {targets},
        # except the skip set. Both leo and ike should be notified.
        assert "leo" in result.delivered
        assert "ike" in result.delivered

    async def test_comment_not_suppressed_for_sender(self, config, mock_db):
        """When Ike comments [from:ike] on a from:leo for:ike issue,
        Leo (the sender) gets notified and Ike (the commenter) is suppressed."""
        labels = ParsedLabels(sender="leo", targets=["ike"], issue_type="task")
        issue = IssueData(number=9, title="[task] Collab", labels=labels)
        comment = CommentData(body="[from:ike] Done with this.", user_login="eandualem")
        event = IssueEvent(event_type=EventType.COMMENT_CREATED, issue=issue, comment=comment)

        with (
            _patch_resolve(),
            _patch_safe_deliver("delivered"),
        ):
            result = await issue_dispatcher(event, config, mock_db)

        # Leo should be delivered (sender, not commenter)
        assert "leo" in result.delivered
        # Ike should be suppressed (commenter removed from target set by
        # _compute_comment_targets, or session-level self-suppression)
        assert "ike" not in result.delivered

    async def test_self_comment_records_acknowledgment(self, config, mock_db):
        """Commenter identified via [from:ike] tag records acknowledgment in DB."""
        labels = ParsedLabels(sender="leo", targets=["ike"], issue_type="task")
        issue = IssueData(number=42, title="[task] Something", labels=labels)
        comment = CommentData(body="[from:ike] Ack", user_login="eandualem")
        event = IssueEvent(event_type=EventType.COMMENT_CREATED, issue=issue, comment=comment)

        with (
            _patch_resolve(),
            _patch_safe_deliver("delivered"),
        ):
            result = await issue_dispatcher(event, config, mock_db)

        # Ike is the commenter and should be skipped (removed from target set)
        assert "ike" not in result.delivered
        # Acknowledgment recorded for the commenter
        mock_db.record_acknowledgment.assert_called_once_with("", 42, "ike")

    async def test_external_comment_clears_acknowledgment(self, config, mock_db):
        """When someone else comments, the target's acknowledgment is cleared
        (new information for them)."""
        labels = ParsedLabels(sender="leo", targets=["ike"], issue_type="task")
        issue = IssueData(number=42, title="[task] Something", labels=labels)
        comment = CommentData(body="[from:leo] New info", user_login="eandualem")
        event = IssueEvent(event_type=EventType.COMMENT_CREATED, issue=issue, comment=comment)

        with (
            _patch_resolve(),
            _patch_safe_deliver("delivered"),
        ):
            result = await issue_dispatcher(event, config, mock_db)

        # Ike should receive the comment (Leo is the commenter, Ike is target)
        assert "ike" in result.delivered
        # Acknowledgment should be cleared for Ike (new info for them)
        mock_db.clear_acknowledgment.assert_called_with("", 42, "ike")

    async def test_repo_local_coding_agent_comment_records_repo_session_ack(self, mock_db):
        registry = EntityRegistry(
            entities={},
            repos=[RepoInfo(org="WF", name="agent-orchestration-dashboard", path="/some/path")],
        )
        config = BackboneConfig(webhook_secret="test-secret", registry=registry)
        issue = IssueData(
            number=20,
            title="Redesign Agent Schedule as calendar view with activity heatmap",
            labels=ParsedLabels(),
            repo_full_name="eandualem/agent-orchestration-dashboard",
        )
        comment = CommentData(body="[from:coding-agent] Acknowledged.", user_login="eandualem")
        event = IssueEvent(event_type=EventType.COMMENT_CREATED, issue=issue, comment=comment)

        with (
            _patch_resolve_sessions(["agent-orchestration-dashboard"]),
            _patch_safe_deliver("delivered"),
        ):
            await issue_dispatcher(event, config, mock_db)

        assert mock_db.record_acknowledgment.await_args_list[0].args == (
            "eandualem/agent-orchestration-dashboard",
            20,
            "coding-agent",
        )
        assert mock_db.record_acknowledgment.await_args_list[1].args == (
            "eandualem/agent-orchestration-dashboard",
            20,
            "agent-orchestration-dashboard",
        )


# ---------------------------------------------------------------------------
# TestCommentRouting — focused tests for the comment dispatch path
# ---------------------------------------------------------------------------


class TestCommentRouting:
    """Tests for the comment-specific dispatch logic: from-tag parsing,
    commenter resolution, target computation, and session-level suppression."""

    async def test_from_tag_suppresses_self_notification(self, config, mock_db):
        """[from:ike] comment on from:leo for:ike issue: ike suppressed, leo notified."""
        labels = ParsedLabels(sender="leo", targets=["ike"], issue_type="task")
        issue = IssueData(number=10, title="[task] Review", labels=labels)
        comment = CommentData(body="[from:ike] Reviewing now.", user_login="eandualem")
        event = IssueEvent(event_type=EventType.COMMENT_CREATED, issue=issue, comment=comment)

        with (
            _patch_resolve(),
            _patch_safe_deliver("delivered"),
        ):
            result = await issue_dispatcher(event, config, mock_db)

        assert "leo" in result.delivered
        # Ike is removed by _compute_comment_targets (commenter subtracted from set)
        assert "ike" not in result.delivered

    async def test_sender_notified_on_comment(self, config, mock_db):
        """Ike comments [from:ike] on from:leo for:ike issue: leo (sender) gets notified."""
        labels = ParsedLabels(sender="leo", targets=["ike"], issue_type="task")
        issue = IssueData(number=11, title="[task] Progress", labels=labels)
        comment = CommentData(body="[from:ike] Making progress.", user_login="eandualem")
        event = IssueEvent(event_type=EventType.COMMENT_CREATED, issue=issue, comment=comment)

        with (
            _patch_resolve(),
            _patch_safe_deliver("delivered") as mock_deliver,
        ):
            result = await issue_dispatcher(event, config, mock_db)

        assert "leo" in result.delivered
        assert mock_deliver.called

    async def test_no_from_tag_falls_back_to_jsonl(self, config, mock_db):
        """No [from:] tag in body, JSONL returns 'ike': ike suppressed as commenter."""
        labels = ParsedLabels(sender="leo", targets=["ike"], issue_type="task")
        issue = IssueData(number=12, title="[task] Fallback", labels=labels)
        comment = CommentData(body="Just a plain comment.", user_login="eandualem")
        event = IssueEvent(event_type=EventType.COMMENT_CREATED, issue=issue, comment=comment)

        with (
            _patch_resolve(),
            _patch_safe_deliver("delivered"),
            _patch_find_outgoing("ike"),
        ):
            result = await issue_dispatcher(event, config, mock_db)

        # ike identified as commenter via JSONL fallback, subtracted from targets
        assert "leo" in result.delivered
        assert "ike" not in result.delivered

    async def test_no_tag_no_jsonl_delivers_to_all(self, config, mock_db):
        """Unknown commenter (no tag, no JSONL): all parties notified."""
        labels = ParsedLabels(sender="leo", targets=["ike"], issue_type="task")
        issue = IssueData(number=13, title="[task] Unknown", labels=labels)
        comment = CommentData(body="Mystery comment.", user_login="eandualem")
        event = IssueEvent(event_type=EventType.COMMENT_CREATED, issue=issue, comment=comment)

        with (
            _patch_resolve(),
            _patch_safe_deliver("delivered"),
            _patch_find_outgoing(None),
        ):
            result = await issue_dispatcher(event, config, mock_db)

        # Both sender and target notified since commenter is unknown
        assert "leo" in result.delivered
        assert "ike" in result.delivered

    async def test_coding_agent_session_level_suppression(self, config, mock_db):
        """Session-level suppression: Ike comments [from:ike] on a for:coding-agent
        issue where coding-agent falls back to 'ike' session. Even though entity
        names differ ('ike' vs 'coding-agent'), the resolved sessions match so
        the target is suppressed."""
        labels = ParsedLabels(sender="leo", targets=["coding-agent"], issue_type="task")
        issue = IssueData(
            number=14,
            title="[task] unknown-repo: Fix dispatch",
            labels=labels,
        )
        comment = CommentData(body="[from:ike] I'll handle this.", user_login="eandualem")
        event = IssueEvent(event_type=EventType.COMMENT_CREATED, issue=issue, comment=comment)

        # coding-agent with no matching repo session falls back to "ike" via _patch_resolve
        with (
            _patch_resolve(),
            _patch_safe_deliver("delivered"),
        ):
            result = await issue_dispatcher(event, config, mock_db)

        # Leo should be delivered (sender)
        assert "leo" in result.delivered
        # coding-agent resolves to ike (fallback), commenter ike also resolves
        # to ike session: session-level self-suppression applies
        assert "coding-agent" in result.skipped

    async def test_elias_skipped_in_comment_routing(self, config, mock_db):
        """for:elias is still skipped even in comment routing."""
        labels = ParsedLabels(sender="ike", targets=["elias"], issue_type="question")
        issue = IssueData(number=15, title="[question] Clarify", labels=labels)
        comment = CommentData(body="[from:leo] Thoughts?", user_login="eandualem")
        event = IssueEvent(event_type=EventType.COMMENT_CREATED, issue=issue, comment=comment)

        with (
            _patch_resolve(),
            _patch_safe_deliver("delivered"),
        ):
            result = await issue_dispatcher(event, config, mock_db)

        # elias is in skip set, so removed by _compute_comment_targets
        assert "elias" not in result.delivered

    async def test_multiple_targets_comment(self, config, mock_db):
        """Feynman comments on from:leo for:ike,feynman issue:
        leo + ike notified, feynman (commenter) suppressed."""
        labels = ParsedLabels(sender="leo", targets=["ike", "feynman"], issue_type="task")
        issue = IssueData(number=16, title="[task] Multi", labels=labels)
        comment = CommentData(body="[from:feynman] I'll handle this.", user_login="eandualem")
        event = IssueEvent(event_type=EventType.COMMENT_CREATED, issue=issue, comment=comment)

        with (
            _patch_resolve(),
            _patch_safe_deliver("delivered"),
        ):
            result = await issue_dispatcher(event, config, mock_db)

        # Feynman is the commenter, removed from target set by _compute_comment_targets
        assert "feynman" not in result.delivered
        # Leo (sender) and Ike (target) should both be notified
        assert "leo" in result.delivered
        assert "ike" in result.delivered

    async def test_duplicate_comment_id_is_suppressed_per_target(self, config, mock_db):
        """The same GitHub comment should not notify the same target twice."""
        labels = ParsedLabels(sender="leo", targets=["ike"], issue_type="task")
        issue = IssueData(number=18, title="[task] Duplicate replay", labels=labels)
        comment = CommentData(
            id=9001,
            body="[from:leo] Same comment delivered twice.",
            user_login="eandualem",
        )
        event = IssueEvent(event_type=EventType.COMMENT_CREATED, issue=issue, comment=comment)

        with (
            _patch_resolve(),
            _patch_safe_deliver("delivered") as mock_deliver,
        ):
            first = await issue_dispatcher(event, config, mock_db)
            second = await issue_dispatcher(event, config, mock_db)

        assert first.delivered == ["ike"]
        assert second.delivered == []
        assert "ike" in second.skipped
        assert mock_deliver.await_count == 1

    async def test_concrete_role_instance_sender_comment_routes_back(self, mock_db):
        """Comments back to a concrete role-instance sender route directly."""
        config = BackboneConfig(
            webhook_secret="test-secret",
            registry=EntityRegistry(
                entities={
                    "bell-wf": EntityEntry(
                        session="bell-wf",
                        home="~/ws/core/code/WF/bell",
                        groups=["orchestrators"],
                        figure="Alexander Graham Bell",
                        role="Org Orchestrator",
                        organization="WF",
                        entity_type="role-instance",
                    ),
                },
                repos=[],
            ),
        )
        labels = ParsedLabels(sender="bell-wf", targets=["coding-agent"], issue_type="bug")
        issue = IssueData(number=17, title="[bug] agent-backbone: Fix dispatch", labels=labels)
        comment = CommentData(body="[from:coding-agent] Done.", user_login="eandualem")
        event = IssueEvent(event_type=EventType.COMMENT_CREATED, issue=issue, comment=comment)

        with (
            patch(
                "agent_backbone.services.routing._resolution.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            _patch_safe_deliver("delivered") as mock_deliver,
        ):
            result = await issue_dispatcher(event, config, mock_db)

        assert result.delivered == ["bell-wf"]
        assert [call.args[0] for call in mock_deliver.await_args_list] == ["bell-wf"]
