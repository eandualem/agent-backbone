"""Tests for flows/issue_dispatcher.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from flows.issue_dispatcher import issue_dispatcher, resolve_session
from src.models import (
    CommentData,
    EventType,
    IssueData,
    IssueEvent,
    ParsedLabels,
    parse_from_tag,
)


def _patch_resolve():
    """Patch resolve_entity_session -- named entities map via config, coding-agent falls back."""

    async def _resolver(target, config, issue_title="", **kwargs):
        if target in config.entities.skip:
            return None
        if target in config.entities.sessions:
            return config.entities.sessions[target]
        return config.entities.fallback.get(target)

    return patch("flows.issue_dispatcher.resolve_entity_session", side_effect=_resolver)


def _patch_safe_deliver(outcome: str = "delivered"):
    """Patch safe_deliver to return a fixed outcome string."""
    return patch(
        "flows.issue_dispatcher.safe_deliver",
        new_callable=AsyncMock,
        return_value=outcome,
    )


def _patch_find_outgoing(result: str | None):
    """Patch find_outgoing_comment to return a fixed value."""
    return patch(
        "flows.issue_dispatcher.find_outgoing_comment",
        return_value=result,
    )


def _patch_db():
    """Patch BackboneDB for persistence assertions. Returns (patcher, mock_db).

    Usage:
        with _patch_db() as (mock_db_cls, mock_db):
            ...
            mock_db.record_acknowledgment.assert_called_once_with(...)
    """

    class _DBContext:
        def __init__(self):
            self.mock_db = AsyncMock()
            self.patcher = patch("src.persistence.BackboneDB")

        def __enter__(self):
            mock_db_cls = self.patcher.__enter__()
            mock_db_cls.return_value.__aenter__ = AsyncMock(return_value=self.mock_db)
            mock_db_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            return mock_db_cls, self.mock_db

        def __exit__(self, *exc):
            return self.patcher.__exit__(*exc)

    return _DBContext()


# ---------------------------------------------------------------------------
# TestParseFromTag
# ---------------------------------------------------------------------------


class TestParseFromTag:
    """Tests for the parse_from_tag() function in src/models.py."""

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
# TestResolveSession
# ---------------------------------------------------------------------------


class TestResolveSession:
    async def test_delegates_to_resolve_entity_session(self):
        """resolve_session delegates to resolve_entity_session from session_bridge."""
        with patch(
            "flows.issue_dispatcher.resolve_entity_session",
            new_callable=AsyncMock,
            return_value="ike",
        ) as mock:
            result = await resolve_session.fn("ike", "[task] Something")
        assert result == "ike"
        mock.assert_called_once()
        args = mock.call_args[0]
        assert args[0] == "ike"
        assert args[2] == "[task] Something"

    async def test_returns_none_when_bridge_returns_none(self):
        """resolve_session returns None when bridge cannot resolve."""
        with patch(
            "flows.issue_dispatcher.resolve_entity_session",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await resolve_session.fn("nobody", "irrelevant")
        assert result is None


# ---------------------------------------------------------------------------
# TestIssueDispatcher — non-comment issue event paths
# ---------------------------------------------------------------------------


class TestIssueDispatcher:
    async def test_dispatch_to_named_entity(self):
        labels = ParsedLabels(sender="leo", targets=["ike"], issue_type="task")
        issue = IssueData(number=1, title="[task] Do thing", labels=labels)
        event = IssueEvent(event_type=EventType.ISSUE_OPENED, issue=issue)

        with (
            _patch_resolve(),
            _patch_safe_deliver("delivered") as mock_deliver,
        ):
            result = await issue_dispatcher.fn(event)

        assert "ike" in result.delivered
        assert mock_deliver.called

    async def test_skip_elias(self):
        labels = ParsedLabels(sender="ike", targets=["elias"], issue_type="question")
        issue = IssueData(number=2, title="[question] Clarify", labels=labels)
        event = IssueEvent(event_type=EventType.ISSUE_OPENED, issue=issue)

        result = await issue_dispatcher.fn(event)
        assert "elias" in result.skipped
        assert result.delivered == []

    async def test_session_offline(self):
        labels = ParsedLabels(sender="leo", targets=["feynman"], issue_type="task")
        issue = IssueData(number=3, title="[task] Something", labels=labels)
        event = IssueEvent(event_type=EventType.ISSUE_OPENED, issue=issue)

        with (
            _patch_resolve(),
            _patch_safe_deliver("delivery_failed"),
        ):
            result = await issue_dispatcher.fn(event)

        assert "feynman" in result.offline

    async def test_multiple_targets(self):
        labels = ParsedLabels(sender="leo", targets=["ike", "feynman"], issue_type="task")
        issue = IssueData(number=5, title="[task] Both", labels=labels)
        event = IssueEvent(event_type=EventType.ISSUE_OPENED, issue=issue)

        with (
            _patch_resolve(),
            _patch_safe_deliver("delivered"),
        ):
            result = await issue_dispatcher.fn(event)

        assert len(result.delivered) == 2

    async def test_ignores_unknown_event(self):
        labels = ParsedLabels(sender="leo", targets=["ike"])
        issue = IssueData(number=6, title="Whatever", labels=labels)
        event = IssueEvent(event_type=EventType.UNKNOWN, issue=issue)

        result = await issue_dispatcher.fn(event)
        assert result.delivered == []
        assert result.offline == []

    async def test_defers_busy_agent(self):
        """Busy agents should get deferred, not delivered."""
        labels = ParsedLabels(sender="leo", targets=["ike"], issue_type="task")
        issue = IssueData(number=7, title="[task] Deferred", labels=labels)
        event = IssueEvent(event_type=EventType.ISSUE_OPENED, issue=issue)

        with (
            _patch_resolve(),
            _patch_safe_deliver("agent_working"),
        ):
            result = await issue_dispatcher.fn(event)

        assert "ike" in result.deferred
        assert result.delivered == []

    async def test_blocking_overrides_processing(self):
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
            result = await issue_dispatcher.fn(event)

        assert "ike" in result.delivered

    async def test_comment_event_unknown_commenter(self):
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
            _patch_db() as (_, mock_db),
        ):
            result = await issue_dispatcher.fn(event)

        # Unknown commenter means nobody is subtracted from {sender} | {targets},
        # except the skip set. Both leo and ike should be notified.
        assert "leo" in result.delivered
        assert "ike" in result.delivered

    async def test_comment_not_suppressed_for_sender(self):
        """When Ike comments [from:ike] on a from:leo for:ike issue,
        Leo (the sender) gets notified and Ike (the commenter) is suppressed."""
        labels = ParsedLabels(sender="leo", targets=["ike"], issue_type="task")
        issue = IssueData(number=9, title="[task] Collab", labels=labels)
        comment = CommentData(body="[from:ike] Done with this.", user_login="eandualem")
        event = IssueEvent(event_type=EventType.COMMENT_CREATED, issue=issue, comment=comment)

        with (
            _patch_resolve(),
            _patch_safe_deliver("delivered"),
            _patch_db() as (_, mock_db),
        ):
            result = await issue_dispatcher.fn(event)

        # Leo should be delivered (sender, not commenter)
        assert "leo" in result.delivered
        # Ike should be suppressed (commenter removed from target set by
        # _compute_comment_targets, or session-level self-suppression)
        assert "ike" not in result.delivered

    async def test_self_comment_records_acknowledgment(self):
        """Commenter identified via [from:ike] tag records acknowledgment in DB."""
        labels = ParsedLabels(sender="leo", targets=["ike"], issue_type="task")
        issue = IssueData(number=42, title="[task] Something", labels=labels)
        comment = CommentData(body="[from:ike] Ack", user_login="eandualem")
        event = IssueEvent(event_type=EventType.COMMENT_CREATED, issue=issue, comment=comment)

        with (
            _patch_resolve(),
            _patch_safe_deliver("delivered"),
            _patch_db() as (_, mock_db),
        ):
            result = await issue_dispatcher.fn(event)

        # Ike is the commenter and should be skipped (removed from target set)
        assert "ike" not in result.delivered
        # Acknowledgment recorded for the commenter
        mock_db.record_acknowledgment.assert_called_once_with(42, "ike")

    async def test_external_comment_clears_acknowledgment(self):
        """When someone else comments, the target's acknowledgment is cleared
        (new information for them)."""
        labels = ParsedLabels(sender="leo", targets=["ike"], issue_type="task")
        issue = IssueData(number=42, title="[task] Something", labels=labels)
        comment = CommentData(body="[from:leo] New info", user_login="eandualem")
        event = IssueEvent(event_type=EventType.COMMENT_CREATED, issue=issue, comment=comment)

        with (
            _patch_resolve(),
            _patch_safe_deliver("delivered"),
            _patch_db() as (_, mock_db),
        ):
            result = await issue_dispatcher.fn(event)

        # Ike should receive the comment (Leo is the commenter, Ike is target)
        assert "ike" in result.delivered
        # Acknowledgment should be cleared for Ike (new info for them)
        mock_db.clear_acknowledgment.assert_called_with(42, "ike")


# ---------------------------------------------------------------------------
# TestCommentRouting — focused tests for the comment dispatch path
# ---------------------------------------------------------------------------


class TestCommentRouting:
    """Tests for the comment-specific dispatch logic: from-tag parsing,
    commenter resolution, target computation, and session-level suppression."""

    async def test_from_tag_suppresses_self_notification(self):
        """[from:ike] comment on from:leo for:ike issue: ike suppressed, leo notified."""
        labels = ParsedLabels(sender="leo", targets=["ike"], issue_type="task")
        issue = IssueData(number=10, title="[task] Review", labels=labels)
        comment = CommentData(body="[from:ike] Reviewing now.", user_login="eandualem")
        event = IssueEvent(event_type=EventType.COMMENT_CREATED, issue=issue, comment=comment)

        with (
            _patch_resolve(),
            _patch_safe_deliver("delivered"),
            _patch_db() as (_, mock_db),
        ):
            result = await issue_dispatcher.fn(event)

        assert "leo" in result.delivered
        # Ike is removed by _compute_comment_targets (commenter subtracted from set)
        assert "ike" not in result.delivered

    async def test_sender_notified_on_comment(self):
        """Ike comments [from:ike] on from:leo for:ike issue: leo (sender) gets notified."""
        labels = ParsedLabels(sender="leo", targets=["ike"], issue_type="task")
        issue = IssueData(number=11, title="[task] Progress", labels=labels)
        comment = CommentData(body="[from:ike] Making progress.", user_login="eandualem")
        event = IssueEvent(event_type=EventType.COMMENT_CREATED, issue=issue, comment=comment)

        with (
            _patch_resolve(),
            _patch_safe_deliver("delivered") as mock_deliver,
            _patch_db() as (_, mock_db),
        ):
            result = await issue_dispatcher.fn(event)

        assert "leo" in result.delivered
        assert mock_deliver.called

    async def test_no_from_tag_falls_back_to_jsonl(self):
        """No [from:] tag in body, JSONL returns 'ike': ike suppressed as commenter."""
        labels = ParsedLabels(sender="leo", targets=["ike"], issue_type="task")
        issue = IssueData(number=12, title="[task] Fallback", labels=labels)
        comment = CommentData(body="Just a plain comment.", user_login="eandualem")
        event = IssueEvent(event_type=EventType.COMMENT_CREATED, issue=issue, comment=comment)

        with (
            _patch_resolve(),
            _patch_safe_deliver("delivered"),
            _patch_find_outgoing("ike"),
            _patch_db() as (_, mock_db),
        ):
            result = await issue_dispatcher.fn(event)

        # ike identified as commenter via JSONL fallback, subtracted from targets
        assert "leo" in result.delivered
        assert "ike" not in result.delivered

    async def test_no_tag_no_jsonl_delivers_to_all(self):
        """Unknown commenter (no tag, no JSONL): all parties notified."""
        labels = ParsedLabels(sender="leo", targets=["ike"], issue_type="task")
        issue = IssueData(number=13, title="[task] Unknown", labels=labels)
        comment = CommentData(body="Mystery comment.", user_login="eandualem")
        event = IssueEvent(event_type=EventType.COMMENT_CREATED, issue=issue, comment=comment)

        with (
            _patch_resolve(),
            _patch_safe_deliver("delivered"),
            _patch_find_outgoing(None),
            _patch_db() as (_, mock_db),
        ):
            result = await issue_dispatcher.fn(event)

        # Both sender and target notified since commenter is unknown
        assert "leo" in result.delivered
        assert "ike" in result.delivered

    async def test_coding_agent_session_level_suppression(self):
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
            _patch_db() as (_, mock_db),
        ):
            result = await issue_dispatcher.fn(event)

        # Leo should be delivered (sender)
        assert "leo" in result.delivered
        # coding-agent resolves to ike (fallback), commenter ike also resolves
        # to ike session: session-level self-suppression applies
        assert "coding-agent" in result.skipped

    async def test_elias_skipped_in_comment_routing(self):
        """for:elias is still skipped even in comment routing."""
        labels = ParsedLabels(sender="ike", targets=["elias"], issue_type="question")
        issue = IssueData(number=15, title="[question] Clarify", labels=labels)
        comment = CommentData(body="[from:leo] Thoughts?", user_login="eandualem")
        event = IssueEvent(event_type=EventType.COMMENT_CREATED, issue=issue, comment=comment)

        with (
            _patch_resolve(),
            _patch_safe_deliver("delivered"),
            _patch_db() as (_, mock_db),
        ):
            result = await issue_dispatcher.fn(event)

        # elias is in skip set, so removed by _compute_comment_targets
        assert "elias" not in result.delivered

    async def test_multiple_targets_comment(self):
        """Feynman comments on from:leo for:ike,feynman issue:
        leo + ike notified, feynman (commenter) suppressed."""
        labels = ParsedLabels(sender="leo", targets=["ike", "feynman"], issue_type="task")
        issue = IssueData(number=16, title="[task] Multi", labels=labels)
        comment = CommentData(body="[from:feynman] I'll handle this.", user_login="eandualem")
        event = IssueEvent(event_type=EventType.COMMENT_CREATED, issue=issue, comment=comment)

        with (
            _patch_resolve(),
            _patch_safe_deliver("delivered"),
            _patch_db() as (_, mock_db),
        ):
            result = await issue_dispatcher.fn(event)

        # Feynman is the commenter, removed from target set by _compute_comment_targets
        assert "feynman" not in result.delivered
        # Leo (sender) and Ike (target) should both be notified
        assert "leo" in result.delivered
        assert "ike" in result.delivered
