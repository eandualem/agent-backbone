"""Finite first-agent setup with durable launch and greeting identities.

This is explicitly requested operator work. No retry schedules itself, adopts an
unrelated agent, weakens runtime trust, or guesses that a lost send was unsent.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from uuid import uuid4

from agent_backbone.base import run_thread
from agent_backbone.config import AgentSpec
from agent_backbone.fs import AtomicCreateUnsupported, atomic_create_text
from agent_backbone.git import detect_repo
from agent_backbone.models import (
    SUCCESS_OUTCOMES,
    BoundSetupFirstAgent,
    SetupCancelFirstAgent,
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
    wait_until_ready,
)
from agent_backbone.services.database import BoundSetupReceipt, SetupReceipt, SetupReceiptError
from agent_backbone.services.routing import DeliveryRefused, envelope, safe_deliver
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

# Setup ends once the CLI is running: what it shows next is the person's to answer.
# A CLI that cannot start quits within moments (Codex 0.160.0: under two seconds).
STARTED_SECONDS = 3.0
# How long the prompt is watched for behind first-run screens. After that the
# delivery-retry job hands over anything still queued.
FIRST_RUN_SECONDS = 600.0

_watches: set[asyncio.Task] = set()
# Closed at shutdown: a setup still finishing then leaves its greeting queued
# for the delivery-retry job instead of starting a watch nothing will stop.
_watches_closed = False

# Keep the original core-v1 prerequisites valid when new defaults are added.
# Existing installations retain their assignments and may have a pending request.
_CORE_V1_POLICIES = (
    "starter-collaboration",
    "starter-continuity",
    "starter-learning",
    "starter-artifacts",
    "starter-authority",
)


def _core(config, spec, brief):
    profile, _ = core_starter_content()
    if not set(_CORE_V1_POLICIES).issubset(config.launch.shared_policy):
        raise SetupReceiptError(
            "starter_conflict", "Assign the core starter policies before adding an agent."
        )
    for name in config.launch.shared_policy:
        if name in _CORE_V1_POLICIES or name in profile["policies"]:
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


async def first_agent(
    request: SetupFirstAgent | BoundSetupFirstAgent,
    config,
    store,
    db,
    *,
    check_effect=None,
    check_admission=None,
    settled=None,
):
    """Run once or reconcile the same explicit request; API authentication is upstream."""
    bound = isinstance(request, BoundSetupFirstAgent)
    if bound and check_effect is None:
        raise ValueError("Bound setup requires provider effect checks")
    if not bound and Path(request.data_dir) != config.data_dir.resolve():
        return setup_result(
            "first_agent", "installation_conflict", "This service owns a different data directory."
        )
    try:
        await _check(check_admission or check_effect)
        receipt_type = BoundSetupReceipt if bound else SetupReceipt
        with receipt_type(config.data_dir, request.model_dump()) as receipt:
            if receipt.cancelled:
                return setup_result(
                    "first_agent",
                    "request_cancelled",
                    "This setup request was cancelled; choose a new request ID.",
                    status="failed",
                    retry="new_request",
                )
            await _check(check_effect)
            receipt.save(agent_name=request.agent.name)
            async with lifecycle_lock(request.agent.name):
                await _check(check_effect)
                return await _first(
                    request, receipt, store, db, check_effect=check_effect, settled=settled
                )
    except SetupReceiptError as exc:
        return setup_result(
            "first_agent",
            exc.code,
            str(exc),
            retry="none" if bound and exc.reason == "receipt_capacity" else None,
        )
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


async def cancel_first_agent(request: SetupCancelFirstAgent, config, *, check_effect=None):
    """Atomically retire an input before effects, including a not-yet-sent input.

    First-agent holds this same nonblocking file lock through all effects. A
    cancellation can therefore never pass a concurrent registration or launch.
    No runtime probing, replay, agent deletion or terminal stop is involved.
    """
    action = request.action
    bound = isinstance(request, BoundSetupFirstAgent)
    if bound and check_effect is None:
        raise ValueError("Bound setup requires provider effect checks")
    if not bound and Path(request.data_dir) != config.data_dir.resolve():
        return setup_result(
            action, "installation_conflict", "This service owns a different data directory."
        )
    try:
        await _check(check_effect)
        receipt_type = BoundSetupReceipt if bound else SetupReceipt
        with receipt_type(config.data_dir, request.original().model_dump()) as receipt:
            if not receipt.cancelled:
                # Preflight can only write these fields. Fail closed for any
                # later or unfamiliar evidence, even an incomplete step.
                allowed = {
                    "schema_version",
                    "request_id",
                    "fingerprint",
                    "completed_steps",
                    "agent_name",
                }
                if receipt.record["completed_steps"] or receipt.record.keys() - allowed:
                    return setup_result(
                        action,
                        "cancellation_unsafe",
                        "Setup may have made changes; the request and any agent were preserved.",
                        retry="none",
                        steps=receipt.record["completed_steps"],
                    )
                receipt.cancel()
            return setup_result(
                action,
                "request_cancelled",
                "This setup request is cancelled; a new request may be submitted.",
                status="completed",
                retry="new_request",
                data={"cancelled_request_id": request.request_id, "can_start_new_request": True},
            )
    except SetupReceiptError as exc:
        return setup_result(
            action,
            exc.code,
            str(exc),
            retry="none" if bound and exc.reason == "receipt_capacity" else None,
        )
    except Exception as exc:
        log.error(
            "Setup cancellation failed request=%s error=%s", request.request_id, type(exc).__name__
        )
        return setup_result(
            action,
            "outcome_unknown",
            "Cancellation was not confirmed; retry the same cancellation request.",
        )


async def _check(check_effect):
    if check_effect is not None:
        await check_effect()


async def _watch_first_run(name, config, runtime, since, settled):
    """Clear the ``starting`` marker at the prompt, then hand over a queued greeting."""
    deadline = time.monotonic() + FIRST_RUN_SECONDS
    while (remaining := deadline - time.monotonic()) > 0:
        outcome, _ = await wait_until_ready(
            name,
            state_dir=config.state_dir,
            runtime=runtime,
            timeout=remaining,
            stale_threshold=config.timing.stale_threshold_seconds,
            since=since,
        )
        if outcome == "ready":
            break
        if outcome != "waiting_for_human":
            return
        await asyncio.sleep(1)
    else:
        return
    if settled is not None:
        await settled(name)
        # Delivery holds a message for the grace period after an idle hook.
        await asyncio.sleep(config.timing.grace_period_seconds + 1)
        await settled(name)


class FirstRunWatches:
    """Cancel first-run watches at shutdown, before the work they hand over stops."""

    async def start(self):
        global _watches_closed
        _watches_closed = False

    async def stop(self):
        global _watches_closed
        _watches_closed = True
        while _watches:
            for task in tuple(_watches):
                task.cancel()
            await asyncio.wait(tuple(_watches))
            await asyncio.sleep(0)

    async def health_check(self):
        return {"healthy": True}


def _watch_ended(task):
    _watches.discard(task)
    if not task.cancelled() and (error := task.exception()) is not None:
        log.warning("First-run watch ended early (%s)", type(error).__name__)


async def _first(request, receipt, store, db, *, check_effect=None, settled=None):
    await _check(check_effect)
    await store.refresh(**({"check_effect": check_effect} if check_effect is not None else {}))
    await _check(check_effect)
    config = store.config
    spec = AgentSpec(
        name=request.agent.name,
        dir=request.agent.directory,
        runtime=request.agent.runtime,
        model=request.agent.model,
        repo=await detect_repo(Path(request.agent.directory)),
    )
    await _check(check_effect)
    if receipt.record.get("result", {}).get("status") == "completed":
        return await _completed_replay(receipt, config, store, check_effect=check_effect)
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
    await _check(check_effect)
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
    ) and (error := await run_thread(selected.setup_error)):
        return _answer(receipt, "runtime_permission", error)
    await _check(check_effect)
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
    await _check(check_effect)
    receipt.completed("preflight")
    if "hooks" not in receipt.record["completed_steps"]:
        for rt in runtimes:
            await _check(check_effect)
            rt.prepare_setup_hooks(config.data_dir, config.state_dir)
        receipt.completed("hooks")
    await _check(check_effect)
    brief_path = _brief_file(receipt, brief)
    if not registration:
        await _check(check_effect)
        path.mkdir(parents=True, exist_ok=True)
        receipt.save(registration_pending=True)

        def registered(created):
            receipt.save(registration_id=created.registration_id, registration_pending=False)
            receipt.completed("agent")

        try:
            spec = await store.register(
                spec,
                only_new=True,
                **(
                    {"check_effect": check_effect, "on_registered": registered}
                    if check_effect is not None
                    else {}
                ),
            )
        except SetupReceiptError:
            # Bound registration records its committed identity before any later
            # effect check. A refusal without that receipt preceded the insert.
            if check_effect is not None and not receipt.record.get("registration_id"):
                receipt.save(registration_pending=False)
            raise
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
        # Preserve the committed identity before a later admission/native check
        # can fail. This records the original effect; it does not authorize another.
        receipt.save(registration_id=spec.registration_id, registration_pending=False)
        receipt.completed("agent")
        await _check(check_effect)
        config = store.config
    operation = receipt.record.get("launch_operation")
    if operation is None:
        if await session_exists(spec.name):
            return _answer(receipt, "agent_conflict", "An existing terminal was preserved.")
        await _check(check_effect)
        operation = uuid4().hex
        receipt.save(launch_operation=operation)
        launched_at = time.time()
        started = await start_agent(
            spec,
            config,
            db=db,
            brief_file=brief_path,
            wait=False,
            operation_id=operation,
            **({"check_effect": check_effect} if check_effect is not None else {}),
        )
        if not started.ok:
            return _answer(
                receipt,
                "launch_failed",
                "The launch did not complete; it will not be repeated automatically.",
                ready="exited",
            )
        await _check(check_effect)
        await store.touch_started(spec.name)
        if not _watches_closed:
            watch = asyncio.create_task(
                _watch_first_run(spec.name, config, spec.runtime, launched_at, settled)
            )
            _watches.add(watch)
            watch.add_done_callback(_watch_ended)
        await asyncio.sleep(STARTED_SECONDS)
        await _check(check_effect)
    # A lost launch response is reconciled from the exact operation and registration,
    # never from the presence of a same-name terminal alone.
    if not await session_exists(spec.name):
        return _answer(
            receipt,
            "launch_failed",
            "The CLI exited right after starting, so the agent is not running. Run the CLI "
            "in a terminal in the agent's folder to see why, then start the agent again.",
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
    await _check(check_effect)
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
    await _check(check_effect)
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
            await _check(check_effect)
            accepted = await _greet(receipt, spec, config, db, check_effect=check_effect)
        if not accepted:
            return _answer(
                receipt,
                "outcome_unknown",
                "Greeting submission is not confirmed; it will not be blindly repeated.",
                ready="ready",
                evidence=evidence,
            )
        await _check(check_effect)
        receipt.completed("greeting")
    final = _answer(
        receipt,
        "agent_ready",
        "Agent started; it may show its own first-run screens in its terminal. "
        "Any greeting receipt records submission only.",
        ready="ready",
        completed=True,
        evidence=evidence,
    )
    await _check(check_effect)
    receipt.save(result=final)
    return final


async def _completed_replay(receipt, config, store, *, check_effect=None):
    await _check(check_effect)
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
    await _check(check_effect)
    return dict(
        receipt.record["result"],
        data={
            **receipt.record["result"]["data"],
            "evidence": [f"state={snapshot.state.value}; source={snapshot.source}"],
        },
    )


async def _greet(receipt, spec, config, db, *, check_effect=None):
    await _check(check_effect)
    operation = receipt.record.get("greeting_operation")
    message = envelope("setup", GREETING)
    if operation:
        rows = await db.deliveries.query(session_name=spec.name, operation_id=operation)
        held = await db.queue.held_receipt(
            spec.name, message, "setup", operation, "", None, "direct_message"
        )
        await _check(check_effect)
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
    try:
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
            **({"check_effect": check_effect} if check_effect is not None else {}),
        )
    except DeliveryRefused as exc:
        # This invocation has proof that neither a paste nor queue admission
        # was attempted. Preserve all other failures as potentially submitted.
        receipt.save(greeting_operation=None)
        raise exc.cause from exc
    await _check(check_effect)
    if (
        not report.unconfirmed
        and report.delivery_id is not None
        and (report.queued or report.outcome in SUCCESS_OUTCOMES)
    ):
        receipt.save(greeting_delivery_id=report.delivery_id)
        return True
    return False
