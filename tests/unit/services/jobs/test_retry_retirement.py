"""Terminal issue failures leave the retry window without losing their history."""

from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.models import IssueData, ParsedLabels
from agent_backbone.services.jobs.retry import delivery_retry
from tests.conftest import TEST_REPO


@pytest.mark.parametrize(
    "outcome", ["acknowledged", "no_repo", "issue_closed", "no_longer_targeted"]
)
async def test_retirement_keeps_history_and_retires_only_the_selected_failure(db, outcome):
    ids = []
    for repo, status in (
        (TEST_REPO, "offline"),
        ("other/repo", "offline"),
        (TEST_REPO, "delivered"),
    ):
        ids.append(
            await db.deliveries.record(
                repo=repo, issue_number=7, target_entity="ike", session_name="ike", outcome=status
            )
        )
    await db.deliveries.retire(ids[0], outcome)
    await db.deliveries.retire(ids[2], outcome)
    rows = {row["id"]: row["outcome"] for row in await db.deliveries.query()}
    assert rows == {ids[0]: outcome, ids[1]: "offline", ids[2]: "delivered"}
    assert [row["id"] for row in await db.deliveries.failed()] == [ids[1]]


async def test_terminal_failures_leave_space_for_later_work(config, db):
    for number in range(1, 22):
        await db.deliveries.record(
            repo=TEST_REPO,
            issue_number=number,
            target_entity="ike",
            session_name="ike",
            outcome="offline",
        )
    gh = AsyncMock()
    gh.get_issue.return_value = IssueData(
        number=1, title="Closed", state="closed", repo_full_name=TEST_REPO
    )
    with patch("agent_backbone.services.jobs.retry.list_sessions", AsyncMock(return_value=[])):
        assert (await delivery_retry(config, db, gh))["issue_closed"] == 20
    assert [row["issue_number"] for row in await db.deliveries.failed()] == [21]
    assert len(await db.deliveries.query(outcome="issue_closed")) == 20


async def test_scope_failure_does_not_abort_other_retries_or_direct_queue(config, db):
    for number in (1, 2):
        await db.deliveries.record(
            repo=TEST_REPO,
            issue_number=number,
            target_entity="ike",
            session_name="ike",
            outcome="offline",
        )
    await db.queue.enqueue(session_name="ike", message="hello", delivery_kind="direct_message")
    gh = AsyncMock()
    gh.get_issue.return_value = IssueData(
        number=1, title="Work", repo_full_name=TEST_REPO, labels=ParsedLabels(targets=["ike"])
    )
    with (
        patch("agent_backbone.services.jobs.retry.list_sessions", AsyncMock(return_value=["ike"])),
        patch(
            "agent_backbone.services.jobs.retry.list_open_queue_for_target",
            AsyncMock(side_effect=[RuntimeError("scope unavailable"), []]),
        ),
        patch(
            "agent_backbone.services.jobs.retry.safe_deliver", AsyncMock(return_value="delivered")
        ) as send,
    ):
        summary = await delivery_retry(config, db, gh)
    assert summary == {"errors": 1, "retried": 1, "queue_delivered": 1}
    assert send.await_count == 2
    assert await db.queue.pending_count("ike") == 0
