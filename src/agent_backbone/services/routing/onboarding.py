"""Finite first-agent setup with durable launch and greeting identities.

This is explicitly requested operator work. No retry schedules itself, adopts an
unrelated agent, weakens runtime trust, or guesses that a lost send was unsent.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from agent_backbone.config import AgentSpec
from agent_backbone.fs import AtomicCreateUnsupported, atomic_create_text
from agent_backbone.git import detect_repo
from agent_backbone.models import (
    SUCCESS_OUTCOMES,
    SetupFirstAgent,
    SetupTemplateBrief,
    setup_result,
)
from agent_backbone.services.agents import (
    AgentRegistrationConflict,
    agent_state,
    lifecycle_lock,
    profile_lock,
    start_agent,
    validate_agent_spec,
)
from agent_backbone.services.database import SetupReceipt, SetupReceiptError
from agent_backbone.services.routing import envelope, safe_deliver
from agent_backbone.services.runtimes import RUNTIMES
from agent_backbone.services.terminal import query_environment_var, session_exists
from agent_backbone.skills import read_store, select_skills
from agent_backbone.templates import core_starter_content, read_template

log = logging.getLogger(__name__)

GREETING = (
    "Introduce yourself briefly as my orchestration agent. Explain how you can help me "
    "coordinate specialist agents, then ask what I want to accomplish first. "
    "Wait for my answer before creating agents, changing projects or making commitments."
)


def _core(config, spec, brief):
    profile, _ = core_starter_content()
    if not set(profile["policies"]).issubset(config.launch.shared_policy):
        raise SetupReceiptError(
            "starter_conflict", "Assign the core starter policies before adding an agent."
        )
    for name in profile["policies"]:
        read_template(name, config.template_dirs)
    store = config.skills.store_path
    selected = select_skills(read_store(store), spec.tags, spec.name) if store else []
    if not set(profile["skills"]).issubset(skill.name for skill in selected):
        raise SetupReceiptError(
            "starter_conflict", "The selected core skills are missing or invalid."
        )
    if isinstance(brief, SetupTemplateBrief):
        path = config.template_dirs.editable / "onboarding/orchestrator-v1.md"
        text = path.read_text()
    else:
        text = brief.text
    if not text.strip() or len(text.encode()) > 32_768:
        raise SetupReceiptError("starter_conflict", "The selected brief is empty or too large.")
    return text


def _brief_file(receipt, text):
    path = receipt.path.with_suffix(".brief.md")
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_text() != text:
            raise SetupReceiptError(
                "starter_conflict", "The saved setup brief changed; it was preserved."
            )
    else:
        atomic_create_text(path, text)
    return path


def _answer(
    receipt, code, message, *, ready="not_started", completed=False, evidence=(), retry=None
):
    return setup_result(
        "first_agent",
        code,
        message,
        status="completed" if completed else "action_required",
        retry="none" if completed else retry,
        steps=receipt.record["completed_steps"],
        data={
            "agent_name": receipt.record.get("agent_name"),
            "launch_id": receipt.record.get("launch_id"),
            "ready": ready,
            "greeting_delivery_id": receipt.record.get("greeting_delivery_id"),
            "evidence": list(evidence),
        },
    )


def _matches_registration(current, spec, registration):
    return (
        current is not None
        and current.registration_id == registration
        and current.dir == spec.dir
        and current.runtime == spec.runtime
        and current.model == spec.model
        and not current.unattended
        and not current.inbox_only
    )


async def first_agent(request: SetupFirstAgent, config, store, db):
    """Run once or reconcile the same explicit request; API authentication is upstream."""
    if Path(request.data_dir) != config.data_dir.resolve():
        return setup_result(
            "first_agent", "installation_conflict", "This service owns a different data directory."
        )
    try:
        with SetupReceipt(config.data_dir, request.model_dump()) as receipt:
            receipt.save(agent_name=request.agent.name)
            async with lifecycle_lock(request.agent.name):
                return await _first(request, receipt, store, db)
    except SetupReceiptError as exc:
        return setup_result("first_agent", exc.code, str(exc))
    except AtomicCreateUnsupported:
        return setup_result(
            "first_agent",
            "filesystem_unsupported",
            "Setup requires a filesystem with hard-link support; choose another data directory.",
            retry="new_request",
        )
    except Exception as exc:
        log.error(
            "First-agent setup failed request=%s error=%s", request.request_id, type(exc).__name__
        )
        return setup_result(
            "first_agent",
            "outcome_unknown",
            "Setup could not confirm the result; inspect the agent and retry the same request.",
        )


async def _first(request, receipt, store, db):
    await store.refresh()
    config = store.config
    spec = AgentSpec(
        name=request.agent.name,
        dir=request.agent.directory,
        runtime=request.agent.runtime,
        model=request.agent.model,
        repo=await detect_repo(Path(request.agent.directory)),
    )
    if receipt.record.get("result", {}).get("status") == "completed":
        return await _completed_replay(receipt, config, store)
    try:
        validate_agent_spec(spec)
    except ValueError:
        return setup_result(
            "first_agent",
            "invalid_request",
            "The agent name, directory or model is invalid.",
            status="failed",
        )
    runtimes = [RUNTIMES.get(name) for name in request.hooks]
    if any(rt is None or not rt.setup_hooks for rt in runtimes):
        return _answer(
            receipt,
            "runtime_permission",
            "Installer hooks are unavailable for a selected runtime; "
            "use its documented agent-start flow.",
            retry="new_request",
        )
    selected = RUNTIMES[spec.runtime]
    if not selected.available():
        return _answer(
            receipt,
            "runtime_missing",
            "Install the selected runtime and make it available to this service.",
        )
    if not any(
        receipt.record.get(key)
        for key in ("registration_id", "registration_pending", "launch_operation")
    ) and (error := selected.setup_error()):
        return _answer(receipt, "runtime_permission", error)
    path = spec.path
    if path.resolve() != path or (path.exists() and not path.is_dir()):
        return _answer(
            receipt, "agent_conflict", "Use an agent directory without symlink redirects."
        )
    # The editable profile is checked, never overwritten or silently assigned.
    try:
        brief = _core(config, spec, request.agent.brief)
    except (OSError, ValueError):
        return _answer(
            receipt,
            "starter_conflict",
            "The core starter profile or selected brief needs attention.",
        )
    current = store.agents.get(spec.name)
    registration = receipt.record.get("registration_id")
    if registration:
        if not _matches_registration(current, spec, registration):
            return _answer(
                receipt, "agent_conflict", "The recorded agent changed; it was preserved."
            )
        spec = current
    else:
        if receipt.record.get("registration_pending"):
            return _answer(
                receipt,
                "outcome_unknown",
                "Registration was interrupted; inspect the registration "
                "before using ordinary agent start.",
                retry="none",
            )
        if any(
            other.name.casefold() == spec.name.casefold() or other.path.resolve() == path
            for other in store.agents
        ) or await session_exists(spec.name):
            return _answer(
                receipt,
                "agent_conflict",
                "An agent or terminal already owns this name or directory.",
            )
    if not path.is_dir() and not request.agent.create_directory:
        return _answer(receipt, "agent_conflict", "The selected agent directory does not exist.")
    receipt.completed("preflight")
    if "hooks" not in receipt.record["completed_steps"]:
        for rt in runtimes:
            rt.prepare_setup_hooks(config.data_dir, config.state_dir)
        receipt.completed("hooks")
    brief_path = _brief_file(receipt, brief)
    if not registration:
        path.mkdir(parents=True, exist_ok=True)
        receipt.save(registration_pending=True)
        try:
            spec = await store.register(spec, only_new=True)
        except AgentRegistrationConflict as exc:
            if exc.registration_id is not None:
                receipt.save(registration_id=exc.registration_id, registration_pending=False)
                receipt.completed("agent")
                if not _matches_registration(
                    store.agents.get(spec.name), spec, exc.registration_id
                ):
                    return _answer(
                        receipt, "agent_conflict", "The recorded agent changed; it was preserved."
                    )
                return _answer(
                    receipt,
                    "agent_conflict",
                    "The created agent changed; inspect it before retrying this request.",
                    retry="same_request",
                )
            receipt.save(registration_pending=False)
            return _answer(
                receipt, "agent_conflict", "An agent already owns this name or directory."
            )
        receipt.save(registration_id=spec.registration_id, registration_pending=False)
        receipt.completed("agent")
        config = store.config
    # Turn off workspace pre-trust for this explicit interactive first launch.
    # Other launch settings and each adapter's sandbox/dialog behavior remain intact.
    config = replace(config, launch=replace(config.launch, pre_trust=False))
    operation = receipt.record.get("launch_operation")
    if operation is None:
        if await session_exists(spec.name):
            return _answer(receipt, "agent_conflict", "An existing terminal was preserved.")
        operation = uuid4().hex
        receipt.save(launch_operation=operation)
        started = await start_agent(
            spec, config, db=db, brief_file=brief_path, wait=True, operation_id=operation
        )
        if not started.ok:
            return _answer(
                receipt,
                "launch_failed",
                "The launch did not complete; it will not be repeated automatically.",
                ready="exited",
            )
        await store.touch_started(spec.name)
    # A lost launch response is reconciled from the exact operation and registration,
    # never from the presence of a same-name terminal alone.
    if not await session_exists(spec.name):
        return _answer(
            receipt,
            "launch_failed",
            "The recorded launch has exited; setup will not restart it.",
            ready="exited",
        )
    actual_operation = await query_environment_var(spec.name, "BACKBONE_OPERATION_ID")
    actual_registration = await query_environment_var(spec.name, "BACKBONE_REGISTRATION_ID")
    launch = await query_environment_var(spec.name, "BACKBONE_LAUNCH_ID")
    if (
        actual_operation != operation
        or actual_registration != spec.registration_id
        or not launch
        or (receipt.record.get("launch_id") not in (None, launch))
    ):
        return _answer(receipt, "agent_conflict", "The terminal does not match this setup launch.")
    receipt.save(launch_id=launch)
    receipt.completed("launch")
    snapshot = await agent_state(config, spec.name)
    # Bounded classifications only: startup panes can contain credentials/login URLs.
    evidence = [f"state={snapshot.state.value}; source={snapshot.source}"]
    if receipt.record.get("result", {}).get("status") == "completed":
        return dict(
            receipt.record["result"],
            data={**receipt.record["result"]["data"], "evidence": evidence},
        )
    if snapshot.state.value == "waiting_for_human":
        return _answer(
            receipt,
            "runtime_permission",
            "Continue in the agent terminal, then retry this request.",
            ready="waiting_for_human",
            evidence=evidence,
        )
    if snapshot.state.value != "idle":
        return _answer(
            receipt,
            "launch_timeout",
            "The runtime is not confirmed ready; inspect its terminal.",
            ready="timeout",
            evidence=evidence,
        )
    receipt.completed("ready")
    if request.start_greeting and receipt.record.get("greeting_delivery_id") is None:
        # Other CLI processes do not share lifecycle_lock. Keep the profile
        # guard from the final identity check through delivery or durable queueing.
        async with profile_lock(config.data_dir):
            if not await db.agents.registration_matches(spec.name, spec.registration_id):
                return _answer(receipt, "agent_conflict", "The greeting recipient changed.")
            for key, field in (
                ("BACKBONE_OPERATION_ID", "launch_operation"),
                ("BACKBONE_REGISTRATION_ID", "registration_id"),
                ("BACKBONE_LAUNCH_ID", "launch_id"),
            ):
                if await query_environment_var(spec.name, key) != receipt.record.get(field):
                    return _answer(receipt, "agent_conflict", "The greeting launch changed.")
            accepted = await _greet(receipt, spec, config, db)
        if not accepted:
            return _answer(
                receipt,
                "outcome_unknown",
                "Greeting submission is not confirmed; it will not be blindly repeated.",
                ready="ready",
                evidence=evidence,
            )
        receipt.completed("greeting")
    final = _answer(
        receipt,
        "agent_ready",
        "Agent started; any greeting receipt records submission only.",
        ready="ready",
        completed=True,
        evidence=evidence,
    )
    receipt.save(result=final)
    return final


async def _completed_replay(receipt, config, store):
    name = receipt.record["agent_name"]
    current = store.agents.get(name)
    if current is None or current.registration_id != receipt.record.get("registration_id"):
        return setup_result(
            "first_agent",
            "agent_conflict",
            "The completed setup registration changed; inspect the current agent.",
            retry="none",
        )
    if not await session_exists(name):
        return _answer(
            receipt,
            "launch_failed",
            "The completed setup launch has exited; use ordinary agent start to launch it again.",
            ready="exited",
        )
    for key, field in (
        ("BACKBONE_OPERATION_ID", "launch_operation"),
        ("BACKBONE_REGISTRATION_ID", "registration_id"),
        ("BACKBONE_LAUNCH_ID", "launch_id"),
    ):
        if await query_environment_var(name, key) != receipt.record.get(field):
            return setup_result(
                "first_agent",
                "agent_conflict",
                "The agent has another launch; setup did not change it.",
                retry="none",
            )
    snapshot = await agent_state(config, name)
    return dict(
        receipt.record["result"],
        data={
            **receipt.record["result"]["data"],
            "evidence": [f"state={snapshot.state.value}; source={snapshot.source}"],
        },
    )


async def _greet(receipt, spec, config, db):
    operation = receipt.record.get("greeting_operation")
    message = envelope("setup", GREETING)
    if operation:
        rows = await db.deliveries.query(session_name=spec.name, operation_id=operation)
        held = await db.queue.held_receipt(
            spec.name, message, "setup", operation, "", None, "direct_message"
        )
        # A queued uncertain delivery is not confirmation. Keep it for inspection.
        delivered = next((row for row in rows if row["outcome"] in SUCCESS_OUTCOMES), None)
        if delivered:
            receipt.save(greeting_delivery_id=delivered["id"])
            return True
        if (
            held
            and held["operation_id"] == operation
            and held["status"] in {"pending", "in_progress", "checkpoint"}
            and rows
        ):
            receipt.save(greeting_delivery_id=rows[0]["id"])
            return True
        return False
    operation = uuid4().hex
    receipt.save(greeting_operation=operation)
    report = await safe_deliver(
        spec.name,
        message,
        config,
        db=db,
        source="setup",
        sender="setup",
        source_key=operation,
        delivery_kind="direct_message",
        operation_id=operation,
        once=True,
    )
    if (
        not report.unconfirmed
        and report.delivery_id is not None
        and (report.queued or report.outcome in SUCCESS_OUTCOMES)
    ):
        receipt.save(greeting_delivery_id=report.delivery_id)
        return True
    return False
