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

    monkeypatch.setattr(onboarding, "detect_repo", AsyncMock(return_value=""))
    monkeypatch.setattr(onboarding, "start_agent", AsyncMock(side_effect=launch))
    monkeypatch.setattr(onboarding, "session_exists", exists)
    monkeypatch.setattr(onboarding, "query_environment_var", environment)
    monkeypatch.setattr(onboarding, "agent_state", snapshot)
    monkeypatch.setattr(type(RUNTIMES[request.param]), "available", lambda self: True)
    monkeypatch.setattr(type(RUNTIMES[request.param]), "installed_version", lambda self: "2.1.4")
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


async def test_new_runtime_install_reaches_first_agent_launch_with_old_daemon_path(
    scenario, db, tmp_path, monkeypatch
):
    from agent_backbone.services.runtimes import base

    body, store, _ = scenario
    runtime = RUNTIMES[body.agent.runtime]
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(base, "_FALLBACK_DIRS", (tmp_path / ".local/bin",))
    monkeypatch.setattr(type(runtime), "available", base.Runtime.available)
    monkeypatch.setattr(type(runtime), "installed_version", base.Runtime.installed_version)
    before = list(store.agents.names)
    assert (await run(scenario, db))["code"] == "runtime_missing"
    assert store.agents.names == before

    directory = ".opencode/bin" if runtime.id == "opencode" else ".local/bin"
    executable = tmp_path / directory / runtime.binary
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nprintf '2.1.4\\n'\n")
    executable.chmod(0o700)
    original_launch = onboarding.start_agent.side_effect
    commands = []

    async def launch(spec, config, **kwargs):
        command = runtime.build_command(
            model=spec.model,
            data_dir=config.data_dir,
            state_dir=config.state_dir,
            brief_file=kwargs["brief_file"],
        )
        assert str(executable) in command
        commands.append(command)
        return await original_launch(spec, config, **kwargs)

    monkeypatch.setattr(onboarding, "start_agent", AsyncMock(side_effect=launch))
    assert runtime.discovery()["available"] is True
    assert (await run(scenario, db))["code"] == "agent_ready"
    assert (await run(scenario, db))["code"] == "agent_ready"
    assert len(commands) == 1 and onboarding.safe_deliver.await_count == 1


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


@pytest.mark.parametrize("outcome", ["delivered", "agent_working", "uncertain"])
async def test_lost_greeting_response_recovers_real_saved_delivery(scenario, db, outcome):
    saved = {}

    async def accepted_then_interrupted(name, message, config, **kwargs):
        operation = kwargs["operation_id"]
        saved["id"] = await db.deliveries.record(
            issue_number=None,
            target_entity=name,
            session_name=name,
            outcome=outcome,
            source="setup",
            kind="direct_message",
            operation_id=operation,
        )
        if outcome != "delivered":
            await db.queue.enqueue(
                session_name=name,
                message=message,
                target_entity=name,
                delivery_kind="direct_message",
                source="setup",
                sender="setup",
                source_key=operation,
                operation_id=operation,
                uncertain=outcome == "uncertain",
            )
        raise OSError("response lost after saved record")

    onboarding.safe_deliver.side_effect = accepted_then_interrupted
    assert (await run(scenario, db))["code"] == "outcome_unknown"
    result = await run(scenario, db)
    if outcome == "uncertain":
        assert result["code"] == "outcome_unknown"
        assert result["data"]["greeting_delivery_id"] is None
    else:
        assert result["code"] == "agent_ready"
        assert result["data"]["greeting_delivery_id"] == saved["id"]
    assert onboarding.safe_deliver.await_count == 1
    assert onboarding.start_agent.await_count == 1


async def test_first_agent_detects_checkout_repository(scenario, db):
    onboarding.detect_repo.return_value = "acme/project"
    assert (await run(scenario, db))["code"] == "agent_ready"
    assert scenario[1].agents.get("guide").repo == "acme/project"


async def test_runtime_preflight_refuses_before_registration(scenario, db, monkeypatch):
    runtime = RUNTIMES[scenario[0].agent.runtime]
    monkeypatch.setattr(type(runtime), "setup_error", lambda self: "Update the selected runtime.")
    result = await run(scenario, db)
    assert result["code"] == "runtime_permission"
    assert not scenario[1].agents.get("guide")
    assert not Path(scenario[0].agent.directory).exists()
    onboarding.start_agent.assert_not_awaited()


@pytest.mark.parametrize("version,refused", [(None, True), ("1.18.32", True), ("2.1.4", False)])
def test_opencode_setup_version_gate(version, refused, monkeypatch):
    runtime = RUNTIMES["opencode"]
    monkeypatch.setattr(type(runtime), "installed_version", lambda self: version)
    assert bool(runtime.setup_error()) is refused


async def test_setup_failure_log_is_classified_without_exception_content(scenario, db, caplog):
    onboarding.start_agent.side_effect = RuntimeError("private-credential-content")
    assert (await run(scenario, db))["code"] == "outcome_unknown"
    assert "RuntimeError" in caplog.text and "first-1" in caplog.text
    assert "private-credential-content" not in caplog.text


async def test_registered_retry_does_not_repeat_runtime_version_preflight(
    scenario, db, monkeypatch
):
    scenario[2]["state"] = AgentState.WAITING_FOR_HUMAN
    assert (await run(scenario, db))["code"] == "runtime_permission"
    runtime = RUNTIMES[scenario[0].agent.runtime]

    def unavailable(self):
        raise AssertionError("version probe must not block an already registered launch")

    monkeypatch.setattr(type(runtime), "setup_error", unavailable)
    scenario[2]["state"] = AgentState.IDLE
    assert (await run(scenario, db))["code"] == "agent_ready"
    assert onboarding.start_agent.await_count == 1


async def test_setup_refuses_registration_replaced_during_store_refresh(scenario, db, monkeypatch):
    body, store, _ = scenario
    refresh = store.refresh
    replaced = False

    async def replace_before_refresh():
        nonlocal replaced
        rows = await db.agents.list()
        if any(row["name"] == body.agent.name for row in rows) and not replaced:
            replaced = True
            # Simulate another operator between register's guard and refresh.
            await db.agents.delete(body.agent.name)
            await db.agents.upsert(
                body.agent.name,
                dir=body.agent.directory + "-replacement",
                runtime=body.agent.runtime,
                model=None,
                repo="",
                tags=[],
                env={},
                description="",
                unattended=False,
            )
        return await refresh()

    monkeypatch.setattr(store, "refresh", replace_before_refresh)
    result = await run(scenario, db)
    assert result["code"] == "agent_conflict" and result["retry"] == "new_request"
    with SetupReceipt(store.config.data_dir, body.model_dump()) as receipt:
        assert (
            receipt.record["registration_id"] != store.agents.get(body.agent.name).registration_id
        )
        assert not receipt.record["registration_pending"]
    assert (await run(scenario, db))["code"] == "agent_conflict"
    assert store.agents.get(body.agent.name).dir.endswith("-replacement")
    onboarding.start_agent.assert_not_awaited()
    onboarding.safe_deliver.assert_not_awaited()


@pytest.mark.parametrize("replace_registration", [True, False])
async def test_setup_rechecks_recipient_after_readiness(
    scenario, db, monkeypatch, replace_registration
):
    body, store, state = scenario
    snapshot = onboarding.agent_state

    async def replace_after_readiness(config, name):
        result = await snapshot(config, name)
        if replace_registration:
            await db.agents.delete(name)
            await db.agents.upsert(
                name,
                dir=body.agent.directory,
                runtime=body.agent.runtime,
                model=None,
                repo="",
                tags=[],
                env={},
                description="",
                unattended=False,
            )
        else:
            state["env"]["BACKBONE_LAUNCH_ID"] = "another-launch"
        return result

    monkeypatch.setattr(onboarding, "agent_state", replace_after_readiness)
    assert (await run(scenario, db))["code"] == "agent_conflict"
    onboarding.safe_deliver.assert_not_awaited()


async def test_greeting_holds_cross_process_guard_through_submission(scenario, db):
    import fcntl
    import os

    async def submit(*args, **kwargs):
        path = scenario[1].config.data_dir / "agent-credentials/.lock"
        fd = os.open(path, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        return DeliveryReport(DeliveryOutcome.DELIVERED, delivery_id=123)

    onboarding.safe_deliver.side_effect = submit
    assert (await run(scenario, db))["code"] == "agent_ready"


async def test_queued_greeting_survives_expiry_and_is_visible_in_inbox(scenario, db):
    saved = {}

    async def enqueue(name, message, config, **kwargs):
        saved["id"] = await db.queue.enqueue(
            session_name=name,
            message=message,
            target_entity=name,
            delivery_kind=kwargs["delivery_kind"],
            source=kwargs["source"],
            sender=kwargs["sender"],
            source_key=kwargs["source_key"],
            operation_id=kwargs["operation_id"],
        )
        return DeliveryReport(DeliveryOutcome.AGENT_WORKING, delivery_id=123, queue="stored")

    onboarding.safe_deliver.side_effect = enqueue
    assert (await run(scenario, db))["code"] == "agent_ready"
    # Negative age makes this fresh row eligible for ordinary notification expiry.
    assert await db.queue.expire_pending(max_age_minutes=-1) == []
    inbox = await db.queue.checkpoint("guide")
    assert len(inbox) == 1 and inbox[0]["id"] == saved["id"].id
    assert inbox[0]["delivery_kind"] == "direct_message"
    assert (await run(scenario, db))["code"] == "agent_ready"
    assert onboarding.safe_deliver.await_count == 1


async def test_post_insert_metadata_change_retains_owned_registration_for_retry(
    scenario, db, monkeypatch
):
    body, store, _ = scenario
    refresh = store.refresh
    edited = False

    async def edit_before_refresh():
        nonlocal edited
        rows = await db.agents.list()
        if any(row["name"] == body.agent.name for row in rows) and not edited:
            edited = True
            await db.agents.update_fields(body.agent.name, {"tags": ["selected"]})
        return await refresh()

    monkeypatch.setattr(store, "refresh", edit_before_refresh)
    result = await run(scenario, db)
    assert result["code"] == "agent_conflict" and result["retry"] == "same_request"
    onboarding.start_agent.assert_not_awaited()
    with SetupReceipt(store.config.data_dir, body.model_dump()) as receipt:
        assert (
            receipt.record["registration_id"] == store.agents.get(body.agent.name).registration_id
        )
    assert (await run(scenario, db))["code"] == "agent_ready"
    assert store.agents.get(body.agent.name).tags == ("selected",)
    with SetupReceipt(store.config.data_dir, body.model_dump()) as receipt:
        assert "agent" in receipt.record["completed_steps"]
        assert "agent" in receipt.record["result"]["completed_steps"]
    assert onboarding.start_agent.await_count == 1
