"""Subscribed source events: matching, batching, and the high-priority path."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from agent_backbone.config import AgentsConfig, AgentSpec, Subscription
from agent_backbone.hooks.backbone_state import take_context
from agent_backbone.models import SUBSCRIPTION_KIND, DeliveryOutcome
from agent_backbone.services.jobs.retry import drain_message_queue
from agent_backbone.services.routing import dispatch_source_events, safe_deliver
from agent_backbone.services.routing._subscriptions import match_subscriptions
from agent_backbone.services.routing.models import SessionIntelligence, SessionProfile
from agent_backbone.services.sources import SourceEvent
from tests.conftest import make_config
from tests.support import queue_row

_DELIVERY = "agent_backbone.services.routing._delivery"
_UPWORK = "from:upwork.com subject:job"
_LINKEDIN = "from:linkedin.com"
_ALEX = "from:linkedin.com alex"


def _event(msg_id: str, *filters: str, subject="New job") -> SourceEvent:
    return SourceEvent(
        source="gmail",
        id=msg_id,
        sender="Upwork <donotreply@upwork.com>",
        subject=subject,
        received_at=datetime(2026, 9, 17, 14, 2, tzinfo=UTC),
        link=f"https://mail.google.com/mail/#all/{msg_id}",
        filters=frozenset(filters),
    )


def _agents(tmp_path) -> AgentsConfig:
    desk = AgentSpec(
        name="desk",
        dir=str(tmp_path / "desk"),
        subscriptions=(
            Subscription(1, "gmail", _UPWORK, "high"),
            Subscription(2, "gmail", _LINKEDIN, "normal"),
            Subscription(3, "gmail", _ALEX, "high"),
        ),
    )
    other = AgentSpec(
        name="other",
        dir=str(tmp_path / "other"),
        subscriptions=(Subscription(4, "gmail", _LINKEDIN, "normal"),),
    )
    return AgentsConfig(specs={"desk": desk, "other": other})


def _config(tmp_path, runtime="claude"):
    agents = _agents(tmp_path)
    if runtime != "claude":
        agents = AgentsConfig(
            specs={name: replace(spec, runtime=runtime) for name, spec in agents.specs.items()}
        )
    return make_config(tmp_path, agents=agents)


def _profile(condition, runtime="claude"):
    return SessionProfile(session_name="desk", intelligence=condition, runtime=runtime)


class TestMatching:
    def test_highest_priority_among_matching_subscriptions(self, tmp_path):
        agents = _agents(tmp_path)
        assert match_subscriptions(agents, _event("1", _LINKEDIN)) == {
            "desk": False,
            "other": False,
        }
        assert match_subscriptions(agents, _event("2", _LINKEDIN, _ALEX)) == {
            "desk": True,
            "other": False,
        }
        assert match_subscriptions(agents, _event("3", "from:nobody")) == {}


class TestDispatch:
    async def test_batches_per_agent_and_priority_and_dedups(self, tmp_path, db):
        config = _config(tmp_path)
        events = [
            _event("a1", _UPWORK),
            _event("a2", _UPWORK),
            _event("l1", _LINKEDIN, subject="Recruiter"),
            _event("a1", _UPWORK),  # the overlap repeats an event
        ]
        with patch(
            "agent_backbone.services.routing._subscriptions.safe_deliver", new_callable=AsyncMock
        ) as deliver:
            deliver.return_value = type(
                "R", (), {"outcome": DeliveryOutcome.DELIVERED, "queue": None, "queued": False}
            )()
            summary = await dispatch_source_events(events, config, db)
        assert summary == {"events": 3, "deduped": 1, "delivered": 3}
        calls = {(c.args[0], c.kwargs["priority"]): c.args[1] for c in deliver.await_args_list}
        assert set(calls) == {("desk", True), ("desk", False), ("other", False)}
        high = calls[("desk", True)].split("\n")
        assert high[0].startswith("[via:gmail]")
        assert [line.split(" · ")[0] for line in high[1:]] == ["- a1", "- a2"]
        assert "https://mail.google.com/mail/#all/a1" in high[1]
        assert "subject «Recruiter»" in calls[("other", False)]
        for call in deliver.await_args_list:
            assert call.kwargs["delivery_kind"] == SUBSCRIPTION_KIND
        stored = await db.events.query(limit=10)
        assert {row["delivery_id"] for row in stored} == {"gmail:a1", "gmail:a2", "gmail:l1"}
        assert all(row["outcome"].startswith("subscription:") for row in stored)

    async def test_an_event_stays_replayable_until_every_recipient_holds_it(self, tmp_path, db):
        config = _config(tmp_path)
        shared = _event("l1", _LINKEDIN)  # desk (normal) and other (normal)

        async def deliver(agent, *args, **kwargs):
            if agent == "other":
                raise RuntimeError("terminal gone")
            return type(
                "R",
                (),
                {"outcome": DeliveryOutcome.AGENT_WORKING, "queue": "stored", "queued": True},
            )()

        with patch(
            "agent_backbone.services.routing._subscriptions.safe_deliver",
            AsyncMock(side_effect=deliver),
        ):
            summary = await dispatch_source_events([shared, _event("a1", _UPWORK)], config, db)
        assert summary == {"events": 2, "queued": 2, "failed": 1, "unprocessed": 1}
        rows = {row["delivery_id"]: row for row in await db.events.query(limit=10)}
        assert rows["gmail:a1"]["processed_at"] is not None
        assert rows["gmail:l1"]["processed_at"] is None
        assert rows["gmail:l1"]["outcome"] == "subscription-partial: desk"

        # The next poll hands the event back for the agent that missed it only.
        with patch(
            "agent_backbone.services.routing._subscriptions.safe_deliver", new_callable=AsyncMock
        ) as deliver:
            deliver.return_value = type(
                "R", (), {"outcome": DeliveryOutcome.DELIVERED, "queue": None, "queued": False}
            )()
            summary = await dispatch_source_events([shared], config, db)
        assert summary == {"events": 1, "delivered": 1}
        assert [c.args[0] for c in deliver.await_args_list] == ["other"]
        row = await db.events.get(rows["gmail:l1"]["id"])
        assert row["processed_at"] is not None and row["outcome"] == "subscription: desk, other"


class TestHighPriorityReachesAWorkingAgent:
    async def test_working_claude_agent_is_offered_hook_context(self, tmp_path, db):
        config = _config(tmp_path)
        message = "[via:gmail] mail\n- a1 · x"
        with (
            patch(
                f"{_DELIVERY}.get_session_intelligence",
                return_value=_profile(SessionIntelligence.AGENT_WORKING),
            ),
            patch(f"{_DELIVERY}.send_message", return_value=True) as send,
        ):
            report = await safe_deliver(
                "desk",
                message,
                config,
                db=db,
                priority=True,
                delivery_kind=SUBSCRIPTION_KIND,
                sender="gmail",
            )
        send.assert_not_awaited()  # never a paste into a busy terminal
        assert report.outcome == DeliveryOutcome.AGENT_WORKING and report.queued
        offer = config.state_dir / "context" / "desk" / f"{report.queue_id}.md"
        assert offer.read_text() == message

        # The agent's hook takes it on its next tool call ...
        assert take_context(config.state_dir, "desk") == [message]
        assert not offer.exists()
        # ... and the drain records the delivery instead of pasting.
        with (
            patch(
                f"{_DELIVERY}.get_session_intelligence",
                return_value=_profile(SessionIntelligence.READY),
            ),
            patch(f"{_DELIVERY}.send_message", return_value=True) as send,
        ):
            summary = await drain_message_queue(config, db, None, active_sessions={"desk"})
        send.assert_not_awaited()
        assert summary.get("queue_delivered") == 1
        assert (await queue_row(db, report.queue_id))["status"] == "delivered"
        receipts = await db.deliveries.query(session_name="desk", kind=SUBSCRIPTION_KIND)
        assert [(r["outcome"], r["source"]) for r in receipts][:1] == [
            ("delivered", "hook-context")
        ]
        assert not list((config.state_dir / "context" / "desk").iterdir())

    async def test_offer_is_withdrawn_when_the_prompt_takes_it_first(self, tmp_path, db):
        config = _config(tmp_path)
        with (
            patch(
                f"{_DELIVERY}.get_session_intelligence",
                return_value=_profile(SessionIntelligence.AGENT_WORKING),
            ),
            patch(f"{_DELIVERY}.send_message", return_value=True),
        ):
            report = await safe_deliver(
                "desk",
                "[via:gmail] mail\n- a1",
                config,
                db=db,
                priority=True,
                delivery_kind=SUBSCRIPTION_KIND,
            )
        offer = config.state_dir / "context" / "desk" / f"{report.queue_id}.md"
        assert offer.exists()
        with (
            patch(
                f"{_DELIVERY}.get_session_intelligence",
                return_value=_profile(SessionIntelligence.READY),
            ),
            patch(f"{_DELIVERY}.send_message", return_value=True) as send,
        ):
            await drain_message_queue(config, db, None, active_sessions={"desk"})
        send.assert_awaited_once()
        assert send.await_args.kwargs.get("runtime_hint") == "claude"
        assert not offer.exists()
        assert take_context(config.state_dir, "desk") == []
        assert (await queue_row(db, report.queue_id))["status"] == "delivered"

    async def test_normal_priority_and_hookless_runtimes_are_not_offered(self, tmp_path, db):
        for runtime, priority in (("claude", False), ("gemini", True)):
            config = _config(tmp_path, runtime=runtime)
            with (
                patch(
                    f"{_DELIVERY}.get_session_intelligence",
                    return_value=_profile(SessionIntelligence.AGENT_WORKING, runtime),
                ),
                patch(f"{_DELIVERY}.send_message", return_value=True),
            ):
                report = await safe_deliver(
                    "desk",
                    "[via:gmail] mail\n- a1",
                    config,
                    db=db,
                    priority=priority,
                    delivery_kind=SUBSCRIPTION_KIND,
                )
            assert report.queued
            assert not (config.state_dir / "context").exists()

    async def test_high_batch_drains_first_and_bypasses_settling(self, tmp_path, db):
        config = _config(tmp_path)
        await db.queue.enqueue(session_name="desk", message="chat", delivery_kind="direct_message")
        await db.queue.enqueue_subscription(
            session_name="desk", header="[via:gmail] mail", lines=["- a1"], priority=1
        )
        with (
            patch(
                f"{_DELIVERY}.get_session_intelligence",
                return_value=_profile(SessionIntelligence.SETTLING),
            ),
            patch(f"{_DELIVERY}.send_message", return_value=True) as send,
        ):
            await drain_message_queue(config, db, None, active_sessions={"desk"})
        send.assert_awaited_once()
        assert send.await_args.args[1].startswith("[via:gmail] mail")
        assert await db.queue.pending_count("desk") == 1  # the chat waits for settling to end
