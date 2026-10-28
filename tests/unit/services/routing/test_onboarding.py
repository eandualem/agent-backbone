"""The same finite setup behavior for every required interactive runtime."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent_backbone.config import AgentSpec
from agent_backbone.models import DeliveryOutcome, SetupFirstAgent
from agent_backbone.services.agents import AgentState, AgentStore, StartResult, StateSnapshot
from agent_backbone.services.database import SetupReceipt
from agent_backbone.services.routing import DeliveryReport, onboarding
from agent_backbone.services.runtimes import RUNTIMES
from agent_backbone.templates import core_starter_content


@pytest.fixture(params=["claude", "codex", "opencode"])
async def scenario(request, db, tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    profile, files = core_starter_content()
    for relative, text in files.items():
        path = data / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    store = AgentStore(db, data)
    await store.start()
    await store.set_setting("agents.shared_policy", profile["policies"])
    await store.set_setting("skills.store", str(data / "skills"))
    body = SetupFirstAgent.model_validate(
        {
            "schema_version": 1,
            "request_id": "first-1",
            "action": "first_agent",
            "data_dir": str(data),
            "hooks": [request.param],
            "start_greeting": True,
            "agent": {
                "name": "guide",
                "directory": str(tmp_path / "project"),
                "create_directory": True,
                "runtime": request.param,
                "model": None,
                "brief": {"template": "orchestrator-v1"},
            },
        }
    )
    state = {"live": False, "env": {}, "state": AgentState.IDLE}

    async def launch(spec, config, **kwargs):
        assert config.launch.pre_trust is False
        assert spec.unattended is False
        assert kwargs["wait"] is True
        assert (
            Path(kwargs["brief_file"]).read_text()
            == (data / "templates/onboarding/orchestrator-v1.md").read_text()
        )
        state["live"] = True
        state["env"] = {
            "BACKBONE_OPERATION_ID": kwargs["operation_id"],
            "BACKBONE_REGISTRATION_ID": spec.registration_id,
            "BACKBONE_LAUNCH_ID": "launch-1",
        }
        return StartResult(True, ready="ready")

    async def exists(name):
        return state["live"]

    async def environment(name, key):
        return state["env"].get(key)

    async def snapshot(config, name):
        return StateSnapshot(state["state"], source="push", evidence=["private terminal text"])

    monkeypatch.setattr(onboarding, "start_agent", AsyncMock(side_effect=launch))
    monkeypatch.setattr(onboarding, "session_exists", exists)
    monkeypatch.setattr(onboarding, "query_environment_var", environment)
    monkeypatch.setattr(onboarding, "agent_state", snapshot)
    monkeypatch.setattr(type(RUNTIMES[request.param]), "available", lambda self: True)
    monkeypatch.setattr(
        onboarding,
        "safe_deliver",
        AsyncMock(return_value=DeliveryReport(DeliveryOutcome.DELIVERED, delivery_id=123)),
    )
    return body, store, state


async def run(scenario, db):
    body, store, _ = scenario
    return await onboarding.first_agent(body, store.config, store, db)


async def test_first_agent_receipt_retries_without_relaunch_or_duplicate_greeting(scenario, db):
    first = await run(scenario, db)
    assert first["code"] == "agent_ready", first
    assert first["data"]["greeting_delivery_id"] == 123
    assert first["data"]["launch_id"] == "launch-1"
    assert "private terminal text" not in str(first)
    assert "greeted" not in first["message"]
    scenario[2]["state"] = AgentState.BUSY
    again = await run(scenario, db)
    assert again["code"] == "agent_ready"
    assert onboarding.start_agent.await_count == 1
    assert onboarding.safe_deliver.await_count == 1
    assert [name for name in scenario[1].agents.names if name.startswith("guide")] == ["guide"]


async def test_permission_wait_is_visible_and_retry_does_not_restart(scenario, db):
    scenario[2]["state"] = AgentState.WAITING_FOR_HUMAN
    first = await run(scenario, db)
    assert first["code"] == "runtime_permission"
    assert first["data"]["ready"] == "waiting_for_human"
    onboarding.safe_deliver.assert_not_awaited()
    scenario[2]["state"] = AgentState.IDLE
    assert (await run(scenario, db))["code"] == "agent_ready"
    assert onboarding.start_agent.await_count == 1
    assert onboarding.safe_deliver.await_count == 1


async def test_no_greeting_without_explicit_request(scenario, db):
    body, store, state = scenario
    scenario = body.model_copy(update={"start_greeting": False}), store, state
    assert (await run(scenario, db))["code"] == "agent_ready"
    onboarding.safe_deliver.assert_not_awaited()


async def test_existing_agent_registration_is_never_adopted(scenario, db):
    body, store, _ = scenario
    await store.register(AgentSpec("guide", body.agent.directory, runtime=body.agent.runtime))
    before = store.agents.get("guide")
    assert (await run(scenario, db))["code"] == "agent_conflict"
    assert store.agents.get("guide") == before
    onboarding.start_agent.assert_not_awaited()


async def test_lost_launch_result_reconciles_matching_operation(scenario, db):
    original = onboarding.start_agent.side_effect

    async def interrupted(*args, **kwargs):
        await original(*args, **kwargs)
        raise OSError("lost response")

    onboarding.start_agent.side_effect = interrupted
    assert (await run(scenario, db))["code"] == "outcome_unknown"
    assert (await run(scenario, db))["code"] == "agent_ready"
    assert onboarding.start_agent.await_count == 1


async def test_lost_launch_with_no_session_is_not_repeated(scenario, db):
    onboarding.start_agent.side_effect = OSError("lost before launch")
    assert (await run(scenario, db))["code"] == "outcome_unknown"
    assert (await run(scenario, db))["code"] == "launch_failed"
    assert onboarding.start_agent.await_count == 1
    onboarding.safe_deliver.assert_not_awaited()


async def test_replaced_launch_is_never_used_for_greeting(scenario, db):
    scenario[2]["state"] = AgentState.WAITING_FOR_HUMAN
    assert (await run(scenario, db))["code"] == "runtime_permission"
    scenario[2]["env"]["BACKBONE_LAUNCH_ID"] = "other-launch"
    assert (await run(scenario, db))["code"] == "agent_conflict"
    onboarding.safe_deliver.assert_not_awaited()


async def test_uncertain_greeting_is_not_repeated(scenario, db):
    onboarding.safe_deliver.side_effect = OSError("send response lost")
    assert (await run(scenario, db))["code"] == "outcome_unknown"
    assert (await run(scenario, db))["code"] == "outcome_unknown"
    assert onboarding.safe_deliver.await_count == 1


async def test_stopped_completed_setup_never_restarts_agent(scenario, db):
    assert (await run(scenario, db))["code"] == "agent_ready"
    scenario[2]["live"] = False
    assert (await run(scenario, db))["code"] == "launch_failed"
    assert onboarding.start_agent.await_count == 1


async def test_request_id_conflict_changes_nothing(scenario, db):
    body, store, state = scenario
    assert (await run(scenario, db))["code"] == "agent_ready"
    changed = body.model_copy(update={"start_greeting": False})
    assert (await run((changed, store, state), db))["code"] == "request_conflict"
    assert onboarding.start_agent.await_count == 1
    assert onboarding.safe_deliver.await_count == 1


async def test_missing_core_never_assigns_or_launches(scenario, db):
    await scenario[1].set_setting("agents.shared_policy", [])
    assert (await run(scenario, db))["code"] == "starter_conflict"
    assert scenario[1].config.launch.shared_policy == ()
    onboarding.start_agent.assert_not_awaited()


async def test_registration_interruption_never_adopts_unknown_registration(scenario, db):
    body, store, _ = scenario
    with SetupReceipt(store.config.data_dir, body.model_dump()) as receipt:
        receipt.save(registration_pending=True)
    assert (await run(scenario, db))["code"] == "outcome_unknown"
    onboarding.start_agent.assert_not_awaited()


@pytest.mark.parametrize("runtime", ["claude", "codex", "opencode"])
def test_preparing_installer_hooks_is_private_and_does_not_modify_user_settings(runtime, tmp_path):
    data = tmp_path / "data"
    home = Path.home()
    before = {str(p): p.read_bytes() for p in home.rglob("*") if p.is_file()}
    RUNTIMES[runtime].prepare_setup_hooks(data, data / "state")
    assert list((data / "hooks").iterdir())
    assert {str(p): p.read_bytes() for p in home.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("runtime", ["gemini", "deepcode", "aider", "shell"])
def test_other_adapters_do_not_claim_installer_support(runtime, tmp_path):
    assert not RUNTIMES[runtime].setup_hooks
    with pytest.raises(RuntimeError):
        RUNTIMES[runtime].prepare_setup_hooks(tmp_path, tmp_path / "state")


async def test_invalid_model_is_rejected_before_registration_and_can_be_corrected(scenario, db):
    body, store, state = scenario
    invalid = body.model_copy(
        update={"agent": body.agent.model_copy(update={"model": "invalid model"})}
    )
    result = await run((invalid, store, state), db)
    assert result["code"] == "invalid_request" and result["retry"] == "new_request"
    assert store.agents.get("guide") is None
    assert not Path(body.agent.directory).exists()
    onboarding.start_agent.assert_not_awaited()


async def test_completed_replay_preserves_later_policy_brief_and_model_edits(
    scenario, db, monkeypatch
):
    body, store, _ = scenario
    assert (await run(scenario, db))["code"] == "agent_ready"
    await store.set_setting("agents.shared_policy", [])
    await store.update("guide", model="another-model")
    (store.config.template_dirs.editable / "onboarding/orchestrator-v1.md").write_text("Edited")
    monkeypatch.setattr(type(RUNTIMES[body.agent.runtime]), "available", lambda self: False)
    assert (await run(scenario, db))["code"] == "agent_ready"
    assert onboarding.start_agent.await_count == 1
    assert onboarding.safe_deliver.await_count == 1


async def test_failed_launch_has_no_endless_retry_instruction(scenario, db):
    onboarding.start_agent.side_effect = OSError("no confirmed launch")
    await run(scenario, db)
    result = await run(scenario, db)
    assert result["code"] == "launch_failed" and result["retry"] == "none"


async def test_unsupported_setup_choice_requires_changed_input(scenario, db):
    body, store, state = scenario
    other = body.model_copy(
        update={"hooks": ["shell"], "agent": body.agent.model_copy(update={"runtime": "shell"})}
    )
    result = await run((other, store, state), db)
    assert result["code"] == "runtime_permission" and result["retry"] == "new_request"
    onboarding.start_agent.assert_not_awaited()


async def test_pending_registration_requires_inspection_not_endless_replay(scenario, db):
    body, store, _ = scenario
    with SetupReceipt(store.config.data_dir, body.model_dump()) as receipt:
        receipt.save(registration_pending=True)
    result = await run(scenario, db)
    assert result["code"] == "outcome_unknown" and result["retry"] == "none"


async def test_interrupted_brief_publication_can_resume_before_registration(
    scenario, db, monkeypatch
):
    publish = onboarding.atomic_create_text
    monkeypatch.setattr(
        onboarding,
        "atomic_create_text",
        lambda *args: (_ for _ in ()).throw(OSError("interrupted")),
    )
    assert (await run(scenario, db))["code"] == "outcome_unknown"
    assert scenario[1].agents.get("guide") is None
    monkeypatch.setattr(onboarding, "atomic_create_text", publish)
    assert (await run(scenario, db))["code"] == "agent_ready"
    assert onboarding.start_agent.await_count == 1
