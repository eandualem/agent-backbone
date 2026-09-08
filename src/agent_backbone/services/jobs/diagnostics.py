"""Report only failing job operations and the next observed successful completion."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.agents import StateSnapshot
    from agent_backbone.services.database import BackboneDB

_incidents: WeakKeyDictionary = WeakKeyDictionary()
_runtime_observations: WeakKeyDictionary = WeakKeyDictionary()


async def observe_job(
    db: BackboneDB,
    *,
    source: str,
    stage: str,
    error_type: str | None = None,
    agent_name: str = "",
    repo: str = "",
    issue_number: int | None = None,
    duration_ms: int | None = None,
) -> None:
    """Coalesce repeated failures of one scoped operation within this process.

    No observation for ordinary success. Recovery means this exact operation
    completed next time; a healthy surrounding scheduler loop cannot recover it.
    A process restart begins a new observation episode, never a guessed recovery.
    """
    incidents = _incidents.setdefault(db, {})
    scope = (source, stage, agent_name, repo, issue_number)
    operation_id = incidents.get(scope)
    if error_type is None and operation_id is None:
        return
    if operation_id is None:
        operation_id = uuid.uuid4().hex
        incidents[scope] = operation_id
    details = {"stage": stage}
    if error_type is not None:
        details["error_type"] = error_type
    if duration_ms is not None:
        details["duration_ms"] = duration_ms
    recorded = await db.diagnostics.record(
        operation_id=operation_id,
        category="job",
        code=f"{stage}_{'failed' if error_type else 'recovered'}",
        severity="error" if error_type else "info",
        source=source,
        agent_name=agent_name,
        repo=repo,
        issue_number=issue_number,
        details=details,
    )
    if error_type is None and recorded is not None:
        incidents.pop(scope, None)


async def observe_runtime(
    db: BackboneDB, config: BackboneConfig, states: dict[str, StateSnapshot]
) -> None:
    """Persist typed terminal observations without treating banner absence as recovery.

    The operation is one episode observed by this monitor, not a provider request
    count. Session identity is held only in memory; no transcript or session ID
    is copied to diagnostics. Restarting the monitor starts a new episode.
    """
    agents = _runtime_observations.setdefault(db, {})
    for missing in agents.keys() - states.keys():
        agents.pop(missing)
    for name, snapshot in states.items():
        if not snapshot.diagnostics_observed:
            continue
        spec = config.agents.get(name)
        if spec is None:
            continue
        generation = (snapshot.runtime, snapshot.session_id)
        previous = agents.get(name)
        if previous is None or previous["generation"] != generation:
            previous = {"generation": generation, "operation_id": uuid.uuid4().hex, "signals": {}}
            agents[name] = previous
        current = set(snapshot.diagnostics)
        if current and not previous["signals"]:
            previous["operation_id"] = uuid.uuid4().hex
        operation_id = previous["operation_id"]
        for signal in snapshot.diagnostics:
            details = {
                "stage": "monitor",
                "state": snapshot.state.value,
                "state_source": snapshot.source,
                "model_source": "configured",
            }
            for key in ("reason", "error_type", "http_status", "observed_effort"):
                value = getattr(signal, key)
                if value is not None:
                    details[key] = value
            if signal.model is not None:
                details["observed_model"] = signal.model
            await db.diagnostics.record(
                operation_id=operation_id,
                category="runtime",
                code=signal.code,
                observation_key=signal.observation_key,
                severity=signal.severity,
                agent_name=name,
                source="agent-monitor",
                runtime=snapshot.runtime or spec.runtime,
                model=spec.model,
                repo=snapshot.current_repo or "",
                issue_number=snapshot.current_issue,
                details=details,
            )
        for old_signal in previous["signals"]:
            if old_signal not in current and old_signal.severity != "info":
                await db.diagnostics.record(
                    operation_id=operation_id,
                    category="runtime",
                    code=f"{old_signal.code}_no_longer_visible",
                    observation_key=old_signal.observation_key,
                    agent_name=name,
                    source="agent-monitor",
                    runtime=snapshot.runtime or spec.runtime,
                    model=spec.model,
                    details={
                        "stage": "monitor",
                        "reason": "banner_absent",
                        "observed_model": old_signal.model,
                        "observed_effort": old_signal.observed_effort,
                        "http_status": old_signal.http_status,
                        "error_type": old_signal.error_type,
                    },
                )
        previous["signals"] = current
