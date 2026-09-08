"""Diagnostic privacy, exact-operation identity and bounded forensic queries."""

from __future__ import annotations

import json

from sqlalchemy import text

from agent_backbone.services.database import BackboneDB, diagnostic_details


async def test_repeated_observation_keeps_correlation_without_copying_payload():
    async with BackboneDB.connect() as db:
        first = await db.diagnostics.record(
            operation_id="message-a",
            category="delivery",
            code="submission_unconfirmed",
            severity="error",
            agent_name="worker",
            repo="acme/a",
            issue_number=7,
            event_id=31,
            queue_id=9,
            delivery_id=5,
            details={
                "condition": "ready",
                "error_type": "RuntimeError",
                "message": "secret-message",
                "evidence": ["secret-terminal"],
            },
        )
        again = await db.diagnostics.record(
            operation_id="message-a",
            category="delivery",
            code="submission_unconfirmed",
            severity="error",
            agent_name="worker",
            details={"condition": "ready"},
        )
        assert first == again and first is not None
        row = await db.diagnostics.get(first)
        assert row["occurrences"] == 2
        assert row["event_id"] == 31 and row["queue_id"] == 9 and row["delivery_id"] == 5
        assert row["repo"] == "acme/a" and row["issue_number"] == 7
        assert row["first_seen_at"] <= row["last_seen_at"]
        assert "secret" not in json.dumps(row)


async def test_unrelated_success_cannot_resolve_failed_operation():
    async with BackboneDB.connect() as db:
        await db.diagnostics.record(
            operation_id="failed-message",
            category="delivery",
            code="submission_unconfirmed",
            severity="error",
            agent_name="worker",
        )
        await db.diagnostics.record(
            operation_id="another-message",
            category="delivery",
            code="submitted",
            agent_name="worker",
        )
        failed = await db.diagnostics.query(operation_id="failed-message")
        assert [row["code"] for row in failed] == ["submission_unconfirmed"]
        assert (await db.diagnostics.digest())["total_groups"] == 1


async def test_digest_groups_before_limiting_and_keeps_repository_identity():
    async with BackboneDB.connect() as db:
        for i, repo in enumerate(("acme/a", "acme/b", "acme/a")):
            await db.diagnostics.record(
                operation_id=f"op-{i}",
                category="delivery",
                code="submission_unconfirmed",
                severity="error",
                agent_name="worker",
                repo=repo,
                issue_number=7,
            )
        for _ in range(3):
            await db.diagnostics.record(
                operation_id="waiting",
                category="delivery",
                code="deferred_agent_working",
                agent_name="worker",
                details={"state": "busy"},
            )
        digest = await db.diagnostics.digest(limit=1)
        assert digest["total_groups"] == 2 and digest["total_occurrences"] == 3
        assert len(digest["groups"]) == 1 and digest["has_more"]
        assert digest["informational"] == {"deferred_agent_working": 3}
        all_groups = (await db.diagnostics.digest())["groups"]
        assert {row["repo"]: row["operation_count"] for row in all_groups} == {
            "acme/a": 2,
            "acme/b": 1,
        }


async def test_legacy_summary_never_selects_message_bodies_or_previews():
    async with BackboneDB.connect() as db:
        await db.deliveries.record(
            target_entity="worker",
            session_name="worker",
            outcome="delivery_failed",
            kind="direct_message",
            preview="secret-preview",
            issue_number=None,
        )
        await db.queue.enqueue(
            session_name="worker", message="secret-body", delivery_kind="direct_message"
        )
        await db.events.record(
            delivery_id="event-1", source="poll", event_type="test", summary="secret-summary"
        )
        result = await db.diagnostics.digest()
        assert result["deliveries"] == {"attempts": 1, "outcomes": {"delivery_failed": 1}}
        assert result["queue"]["pending"] == 1 and result["queue"]["oldest_pending_at"]
        assert "secret" not in json.dumps(result)
        assert result["coverage"]["earliest_retained_at"] is None


async def test_recency_filter_is_explicitly_lifetime_counts_and_prune_uses_last_seen():
    async with BackboneDB.connect() as db:
        active = await db.diagnostics.record(
            operation_id="active",
            category="startup",
            code="timeout",
            severity="warning",
        )
        old = await db.diagnostics.record(
            operation_id="old",
            category="startup",
            code="exited",
            severity="error",
        )
        async with db.engine.begin() as conn:
            await conn.execute(
                text("UPDATE diagnostics SET first_seen_at='2000-01-01T00:00:00.000000Z'")
            )
            await conn.execute(
                text(
                    "UPDATE diagnostics SET last_seen_at='2000-01-01T00:00:00.000000Z' WHERE id=:id"
                ),
                {"id": old},
            )
        result = await db.diagnostics.digest(since="2020-01-01T00:00:00.000000Z")
        assert result["total_groups"] == 1
        assert "retained occurrences" in result["count_semantics"]
        assert result["groups"][0]["first_seen_at"].startswith("2000-")
        assert await db.diagnostics.prune(30) == 1
        assert await db.diagnostics.get(old) is None
        assert await db.diagnostics.get(active) is not None


async def test_recorder_failure_is_nonfatal_and_logs_no_exception_payload(monkeypatch, caplog):
    async with BackboneDB.connect() as db:

        def fail():
            raise RuntimeError("secret-sql-parameters")

        monkeypatch.setattr(db.diagnostics, "_tx", fail)
        monkeypatch.setattr(type(db.diagnostics), "_last_warning", float("-inf"))
        before = type(db.diagnostics).write_failures
        assert await db.diagnostics.record(operation_id="x", category="job", code="failed") is None
        assert type(db.diagnostics).write_failures == before + 1
        assert "evidence is incomplete" in caplog.text
        assert "secret" not in caplog.text


def test_details_allowlist_rejects_free_text_nested_values_and_control_characters():
    assert diagnostic_details(
        {
            "error_type": "ValueError",
            "http_status": 400,
            "resume": False,
            "reason": "a sentence containing a credential",
            "stage": "bad\nvalue",
            "duration_ms": True,
            "model_source": {"secret": "nested"},
            "message": "secret",
        }
    ) == {"error_type": "ValueError", "http_status": 400, "resume": False}


async def test_invalid_optional_request_metadata_does_not_drop_failure(db):
    record_id = await db.diagnostics.record(
        operation_id="request",
        category="startup",
        code="failed",
        severity="error",
        agent_name="bad name\n",
        runtime="bad runtime",
        model="secret free text",
        details={"error_type": "ValueError", "requested_model": "secret free text"},
    )
    row = await db.diagnostics.get(record_id)
    assert row["agent_name"] == row["runtime"] == ""
    assert row["model"] is None
    assert row["details"] == {"error_type": "ValueError"}
