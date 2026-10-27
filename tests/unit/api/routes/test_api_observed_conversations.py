"""Skills and policies as conversation artifacts (docs/conversations.md)."""

import os
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from agent_backbone.config import SkillsConfig
from tests.unit.api.routes.test_api_conversations import AGENT, _queued


def _skill_md(name: str, body: str = "# x") -> str:
    return f"---\nname: {name}\ndescription: d {name}\n---\n{body}\n"


@pytest.fixture
def deliver(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr("agent_backbone.api.routes.conversations.deliver_now", mock)
    return mock


@pytest.fixture
def store(api_app, tmp_path):
    store = tmp_path / "store"
    (store / "review").mkdir(parents=True)
    (store / "review" / "SKILL.md").write_text(_skill_md("review"))
    api_app.state.config = replace(api_app.state.config, skills=SkillsConfig(store=str(store)))
    return store


async def _skills(client, headers) -> dict:
    body = (await client.get("/api/skills", headers=headers)).json()
    return {item["name"]: item for item in body["items"]}


@pytest.mark.parametrize("runtime", ("claude", "codex", "opencode", "gemini", "deepcode"))
async def test_skill_conversation_round_trip(
    api_app, api_client, auth_headers, deliver, store, runtime
):
    db = api_app.state.db
    await db.agents.update_fields("ike", {"runtime": runtime})
    artifact_id = (await _skills(api_client, auth_headers))["review"]["artifact_id"]
    assert artifact_id == (await _skills(api_client, auth_headers))["review"]["artifact_id"]
    path = f"/api/conversations/skill/{artifact_id}"

    empty = (await api_client.get(path, headers=auth_headers)).json()
    assert empty["entries"] == [] and empty["source_status"] == "active"
    assert empty["responsible"] == {"agent_id": None, "name": None, "status": "unassigned"}
    refused = await api_client.post(
        path + "/messages", headers=auth_headers, json={"request_id": "a", "text": "Why?"}
    )
    assert refused.json()["error"] == "recipient_required"

    ask = {"request_id": "a", "text": "Why this rule?", "recipient": "ike"}
    sent = await api_client.post(path + "/messages", headers=auth_headers, json=ask)
    assert sent.status_code == 201, sent.text
    entry = sent.json()["entry"]
    assert deliver.await_args.args[3] == "ike"
    row = await _queued(db, entry["id"])
    assert row["message"].startswith(
        f'[via:backbone conversation:skill/{artifact_id} from:owner] On "review" (v1):'
    )
    reply = {"request_id": "r", "author": "ike", "text": "Because.", "in_reply_to": entry["id"]}
    assert (await api_client.post(path + "/messages", headers=AGENT, json=reply)).status_code == 201

    # An observed change is a new version and a revision event by Backbone; an
    # agent cannot claim it as the version its reply produced.
    (store / "review" / "SKILL.md").write_text(_skill_md("review", "# y"))
    page = (await api_client.get(path, headers=auth_headers)).json()
    event = page["entries"][-1]
    assert event["kind"] == "revision" and event["revision"]["label"] == "v2"
    assert event["author"] == {"type": "backbone", "agent_id": None, "name": "backbone"}
    assert page["latest_revision_id"] == event["revision"]["revision_id"]
    claim = reply | {"request_id": "c", "resulting_revision_id": page["latest_revision_id"]}
    claimed = await api_client.post(path + "/messages", headers=AGENT, json=claim)
    assert claimed.json()["error"] == "invalid_revision"

    versions = (
        await api_client.get(f"/api/skills/{artifact_id}/versions", headers=auth_headers)
    ).json()["items"]
    assert [v["label"] for v in versions] == ["v2", "v1"]
    old = await api_client.get(
        f"/api/skills/{artifact_id}/versions/{versions[1]['revision_id']}", headers=auth_headers
    )
    assert old.json()["content"] == _skill_md("review")


async def test_missing_keeps_identity_and_retirement_starts_a_new_one(
    api_client, auth_headers, store
):
    first = (await _skills(api_client, auth_headers))["review"]["artifact_id"]
    path = f"/api/conversations/skill/{first}"
    (store / "review" / "SKILL.md").unlink()
    assert (await api_client.get(path, headers=auth_headers)).json()["source_status"] == "missing"
    (store / "review" / "SKILL.md").write_text(_skill_md("review"))
    page = (await api_client.get(path, headers=auth_headers)).json()
    assert page["source_status"] == "active" and page["entries"] == []  # same content: no version
    assert (await _skills(api_client, auth_headers))["review"]["artifact_id"] == first

    assert (await api_client.post(path + "/retire", headers=AGENT)).status_code == 403
    retired = await api_client.post(path + "/retire", headers=auth_headers)
    assert retired.json()["status"] == "retired"
    second = (await _skills(api_client, auth_headers))["review"]["artifact_id"]
    assert second != first
    assert (await api_client.get(path, headers=auth_headers)).json()["source_status"] == "retired"


async def test_a_fifo_skill_file_is_missing_not_a_hang(api_client, auth_headers, store):
    (store / "pipe").mkdir()
    os.mkfifo(store / "pipe" / "SKILL.md")
    skills = await _skills(api_client, auth_headers)
    assert skills["pipe"]["artifact_id"] is None and skills["review"]["artifact_id"]


async def test_maintainer_mapping_is_explicit_and_operator_only(
    api_app, api_client, auth_headers, deliver, store
):
    artifact_id = (await _skills(api_client, auth_headers))["review"]["artifact_id"]
    path = f"/api/conversations/skill/{artifact_id}"
    body = {"agent": "ike"}
    assert (await api_client.put(path + "/maintainer", headers=AGENT, json=body)).status_code == 403
    unknown = await api_client.put(path + "/maintainer", headers=auth_headers, json={"agent": "x"})
    assert unknown.json()["error"] == "unknown_recipient"
    mapped = await api_client.put(path + "/maintainer", headers=auth_headers, json=body)
    assert mapped.json()["responsible"]["name"] == "ike"
    assert mapped.json()["responsible"]["status"] == "active"

    sent = await api_client.post(
        path + "/messages", headers=auth_headers, json={"request_id": "a", "text": "Hi"}
    )
    assert sent.json()["entry"]["recipient"]["name"] == "ike"
    mine = (await api_client.get("/api/conversations?agent=ike", headers=AGENT)).json()
    assert [item["artifact_id"] for item in mine["items"]] == [artifact_id]

    async def moved():
        async with api_app.state.db.engine.begin() as conn:
            return await conn.scalar(
                text("SELECT updated_at FROM conversation_reads WHERE artifact_id = :id"),
                {"id": artifact_id},
            )

    before = await moved()
    cleared = await api_client.delete(path + "/maintainer", headers=auth_headers)
    assert cleared.json()["responsible"]["status"] == "unassigned"
    assert await moved() > before  # summary polling reports the change
    document = await api_client.put(
        "/api/conversations/document/" + "0" * 32 + "/maintainer", headers=auth_headers, json=body
    )
    assert document.status_code == 404 and document.json()["error"] == "unknown_artifact"


async def test_policy_identity_survives_an_override(api_app, api_client, auth_headers):
    policies = (await api_client.get("/api/policies", headers=auth_headers)).json()["items"]
    policy = next(item for item in policies if item["name"] == "starter-continuity")
    assert policy["status"] == "active" and policy["latest_revision_id"]

    override = api_app.state.config.template_dirs.editable / "policies" / "starter-continuity.md"
    override.parent.mkdir(parents=True)
    override.write_text("Keep notes.\n")
    (override.parent / "my--policy.md").write_text("Allowed by the policy name rule.\n")
    base = f"/api/policies/{policy['artifact_id']}/versions"
    versions = (await api_client.get(base, headers=auth_headers)).json()["items"]
    assert [v["label"] for v in versions] == ["v2", "v1"]
    latest = await api_client.get(f"{base}/{versions[0]['revision_id']}", headers=auth_headers)
    assert latest.json()["content"] == "Keep notes.\n"
    again = (await api_client.get("/api/policies", headers=auth_headers)).json()["items"]
    assert policy["artifact_id"] in {item["artifact_id"] for item in again}
    assert {item["name"]: item for item in again}["starter-continuity"]["path"] == str(override)
    assert "my--policy" in {item["name"] for item in again}
    (override.parent / "blank.md").write_text("  \n")
    blank = (await api_client.get("/api/policies", headers=auth_headers)).json()["items"]
    assert {"name": "blank", "artifact_id": None, "status": "missing"}.items() <= next(
        item for item in blank if item["name"] == "blank"
    ).items()
    assert (await api_client.get(f"{base}/999", headers=auth_headers)).status_code == 404


async def test_a_failed_snapshot_still_relinks_agents(
    api_app, api_client, auth_headers, store, monkeypatch
):
    relink = AsyncMock(return_value=[])
    monkeypatch.setattr("agent_backbone.api.routes.skills.relink_skills", relink)
    monkeypatch.setattr(
        "agent_backbone.api.routes.skills.commit_store", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        api_app.state.db.conversations, "observe", AsyncMock(side_effect=RuntimeError("db down"))
    )
    tagged = await api_client.put(
        "/api/skills/review/tags", headers=auth_headers, json={"tags": ["all"], "actor": "ike"}
    )
    assert tagged.status_code == 200, tagged.text
    assert tagged.json()["warnings"] == ["conversation version not recorded: db down"]
    relink.assert_awaited_once()
