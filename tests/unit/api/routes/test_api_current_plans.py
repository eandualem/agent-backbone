"""Current checklists are authored data with atomic updates and feedback context."""

from copy import deepcopy
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import text

from agent_backbone.config import agent_api_key
from agent_backbone.models import CURRENT_PLAN_BODY_BYTES
from tests.report_support import author


def publication(identity, *, author="ike"):
    return {
        "author": author,
        "author_id": identity,
        "request_id": uuid4().hex,
        "base_revision_id": None,
        "plan": {
            "plan_id": uuid4().hex,
            "title": "Current work",
            "items": [
                {
                    "item_id": uuid4().hex,
                    "text": "Check the change",
                    "section": "now",
                    "status": "in_progress",
                    "detail": "",
                    "references": [],
                }
            ],
        },
    }


async def discover(client, headers, name="ike"):
    response = await client.get("/api/current-plans", params={"agent": name}, headers=headers)
    assert response.status_code == 200
    return response.json()["items"][0]


@pytest.mark.parametrize(
    "runtime", ("claude", "codex", "opencode", "gemini", "deepcode", "aider", "shell")
)
async def test_current_plan_same_runtime_contract(api_app, api_client, auth_headers, runtime):
    await api_app.state.db.agents.update_fields("ike", {"runtime": runtime})
    entry = await discover(api_client, auth_headers)
    assert entry["record"] is None
    body = publication(entry["author_id"])
    first = await api_client.post("/api/current-plans", headers=auth_headers, json=body)
    assert first.status_code == 201
    original = first.json()["receipt"]
    changed = deepcopy(body)
    changed.update(request_id=uuid4().hex, base_revision_id=original["revision_id"])
    changed["plan"]["items"][0]["status"] = "done"
    second = await api_client.post("/api/current-plans", headers=auth_headers, json=changed)
    assert second.status_code == 201
    retry = await api_client.post("/api/current-plans", headers=auth_headers, json=body)
    assert retry.status_code == 200 and retry.json() == {"created": False, "receipt": original}
    stale = deepcopy(body)
    stale["request_id"] = uuid4().hex
    refusal = await api_client.post("/api/current-plans", headers=auth_headers, json=stale)
    assert refusal.status_code == 409 and refusal.json()["detail"]["code"] == "stale_revision"
    read = await api_client.get("/api/current-plans/" + entry["author_id"], headers=auth_headers)
    assert read.json()["plan"] == changed["plan"]
    assert not read.json()["stale"]
    assert "content" not in original
    # Freshness is evidence, never a date-boundary reset of unfinished work.
    async with api_app.state.db.engine.begin() as conn:
        await conn.execute(text("UPDATE current_plans SET updated_at='2020-01-01T00:00:00+00:00'"))
    read = await api_client.get("/api/current-plans/" + entry["author_id"], headers=auth_headers)
    assert read.json()["stale"] and read.json()["plan"]["items"] == changed["plan"]["items"]


async def test_feedback_is_atomic_contextual_and_never_redirected(
    api_app, api_client, auth_headers
):
    entry = await discover(api_client, auth_headers)
    identity = entry["author_id"]
    body = publication(identity)
    first = (await api_client.post("/api/current-plans", headers=auth_headers, json=body)).json()[
        "receipt"
    ]
    feedback = {
        "author": None,
        "request_id": uuid4().hex,
        "observed_plan_id": first["plan_id"],
        "observed_revision_id": first["revision_id"],
        "item_ids": [body["plan"]["items"][0]["item_id"]],
        "kind": "question",
        "text": "What remains?\n\tPlease explain.",
    }
    path = f"/api/current-plans/{identity}/feedback"
    with patch("agent_backbone.api.routes.current_plans.deliver_now", AsyncMock()):
        sent = await api_client.post(path, headers=auth_headers, json=feedback)
        assert sent.status_code == 200
        receipt = sent.json()
        async with api_app.state.db.engine.begin() as conn:
            queued = (
                (
                    await conn.execute(
                        text("SELECT * FROM message_queue WHERE operation_id=:id"),
                        {"id": receipt["operation_id"]},
                    )
                )
                .mappings()
                .one()
            )
        assert queued["registration_id"] == identity
        assert "Check the change" in queued["message"] and first["revision_id"] in queued["message"]
        assert receipt["delivery"]["queued"]
        body.update(request_id=uuid4().hex, base_revision_id=first["revision_id"])
        body["plan"]["plan_id"] = uuid4().hex
        body["plan"]["items"] = []
        assert (
            await api_client.post("/api/current-plans", headers=auth_headers, json=body)
        ).status_code == 201
        stale = feedback | {"request_id": uuid4().hex}
        assert (await api_client.post(path, headers=auth_headers, json=stale)).status_code == 409
        again = (await api_client.post(path, headers=auth_headers, json=feedback)).json()
        assert again["operation_id"] == receipt["operation_id"]
        await api_app.state.db.agents.delete("ike")
        await author(api_app.state.db, "ike")
        again = (await api_client.post(path, headers=auth_headers, json=feedback)).json()
        assert again["operation_id"] == receipt["operation_id"]
        assert again["delivery"]["outcome"] == "expired"
        assert not again["delivery"]["session"]
        assert (await api_client.post(path, headers=auth_headers, json=stale)).json()["detail"][
            "code"
        ] == "owner_unavailable"
    assert (await discover(api_client, auth_headers))["record"] is None
    assert (
        await api_client.get(f"/api/current-plans/{identity}", headers=auth_headers)
    ).status_code == 404
    async with api_app.state.db.engine.begin() as conn:
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM message_queue WHERE source='current-plan-feedback'")
            )
            == 1
        )


async def test_auth_validation_conflicts_and_feedback_privileges(api_app, api_client, auth_headers):
    identity = (await discover(api_client, auth_headers))["author_id"]
    body = publication(identity)
    for path in (
        "/api/current-plans",
        "/api/current-plans/schema",
        f"/api/current-plans/{identity}",
    ):
        assert (await api_client.get(path)).status_code == 401
    large = b"x" * (CURRENT_PLAN_BODY_BYTES + 1)
    assert (
        await api_client.post("/api/current-plans", headers=auth_headers, content=large)
    ).status_code == 413
    invalid = deepcopy(body)
    invalid["plan"]["title"] = "PRIVATE_REJECTED" * 100
    response = await api_client.post("/api/current-plans", headers=auth_headers, json=invalid)
    assert response.status_code == 422 and "PRIVATE_REJECTED" not in response.text
    invalid = deepcopy(body)
    invalid["plan"]["items"] *= 2
    assert (
        await api_client.post("/api/current-plans", headers=auth_headers, json=invalid)
    ).status_code == 422
    first = (await api_client.post("/api/current-plans", headers=auth_headers, json=body)).json()[
        "receipt"
    ]
    changed = deepcopy(body)
    changed["plan"]["title"] = "Changed"
    response = await api_client.post("/api/current-plans", headers=auth_headers, json=changed)
    assert response.status_code == 409 and response.json()["detail"]["code"] == "request_conflict"
    feedback = {
        "author": None,
        "request_id": uuid4().hex,
        "observed_plan_id": first["plan_id"],
        "observed_revision_id": first["revision_id"],
        "kind": "feedback",
        "text": "Continue",
    }
    scoped = {"Authorization": "Bearer " + agent_api_key(api_app.state.config.api_key)}
    assert (
        await api_client.post(
            f"/api/current-plans/{identity}/feedback", headers=scoped, json=feedback
        )
    ).status_code == 403
    feedback["text"] = "unsafe\x1b[2J"
    assert (
        await api_client.post(
            f"/api/current-plans/{identity}/feedback", headers=auth_headers, json=feedback
        )
    ).status_code == 422
