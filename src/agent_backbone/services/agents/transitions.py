"""Explicit one-time transitions: an agent (or a person) asks the backbone to
stop a session and, after a wait, start its replacement.

The request is validated and persisted here; nothing is stopped inside the
request, so a caller asking for its own session survives to read the answer.
``services.jobs.transitions`` performs the stop and the start.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from agent_backbone.services.database import format_iso, parse_iso
from agent_backbone.services.runtimes import RUNTIMES

if TYPE_CHECKING:
    from agent_backbone.config import AgentSpec, BackboneConfig
    from agent_backbone.services.database import BackboneDB

DEFAULT_DELAY_SECONDS = 60
SOURCE = "agent-restart"
"""Delivery source of the continuation message; the queue never expires it."""


@dataclass(frozen=True)
class TransitionRequest:
    """What ``agent restart`` was asked to do."""

    runtime: str | None = None
    model: str | None = None
    resume: bool = False
    start: bool = True
    """False for stop-only."""
    delay_seconds: int | None = None
    """Seconds between the stop and the start; default ``DEFAULT_DELAY_SECONDS``."""
    start_at: str | None = None
    """An explicit start time (ISO 8601); excludes ``delay_seconds``."""
    message: str | None = None
    requested_by: str = ""


class TransitionPending(ValueError):
    """The agent already has an open transition; carries its row."""

    def __init__(self, row: dict) -> None:
        super().__init__(
            f"'{row['agent_name']}' already has a pending restart (#{row['id']}); "
            "let it finish, or start the agent by hand to supersede it"
        )
        self.row = row


def parse_start_at(value: str) -> str:
    """An ISO 8601 time (naive = local time) as a stored UTC timestamp."""
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(f"start_at '{value}' is not an ISO 8601 time") from None
    if moment.tzinfo is None:
        moment = moment.astimezone()  # local time, as a person would write it
    return format_iso(moment)


def validate_transition(config: BackboneConfig, spec: AgentSpec, req: TransitionRequest) -> None:
    """Refuse a request that could not work, before anything is torn down (``ValueError``)."""
    if spec.name == config.backbone.session_name:
        raise ValueError("refusing to restart the backbone's own session")
    if req.delay_seconds is not None and req.start_at is not None:
        raise ValueError("give a delay or a start time, not both")
    if req.delay_seconds is not None and req.delay_seconds < 0:
        raise ValueError("the delay cannot be negative")
    if req.start_at is not None:
        parse_iso(req.start_at)
    if not req.start:
        if req.delay_seconds is not None or req.start_at is not None or req.resume:
            raise ValueError("stop-only has no start: drop the timing and resume options")
        return
    runtime = req.runtime or spec.runtime
    if runtime not in RUNTIMES:
        raise ValueError(f"Unknown runtime: {runtime}")
    rt = RUNTIMES[runtime]
    if not rt.available():
        raise ValueError(f"Runtime '{runtime}' binary not found")
    if req.resume and runtime != spec.runtime:
        raise ValueError(
            f"resume continues a conversation in the same CLI: the agent runs {spec.runtime}, "
            f"{runtime} was requested — start fresh, or keep the CLI"
        )
    model = req.model if req.model is not None else spec.model if runtime == spec.runtime else None
    model_id, effort = rt.split_model(model)
    if effort and not model_id:
        raise ValueError(f"model spec '{model}' names an effort but no model")
    try:
        rt.check_effort(effort)
    except RuntimeError as exc:
        raise ValueError(str(exc)) from None
    if not spec.path.is_dir():
        raise ValueError(f"Directory does not exist: {spec.path}")


async def request_transition(
    db: BackboneDB, config: BackboneConfig, spec: AgentSpec, req: TransitionRequest
) -> dict:
    """Validate and persist the request as ``pending``; the job executes it."""
    validate_transition(config, spec, req)
    open_row = await db.transitions.open_for(spec.name)
    if open_row is not None:
        raise TransitionPending(open_row)
    return await db.transitions.create(
        agent_name=spec.name,
        requested_by=req.requested_by,
        runtime=req.runtime,
        model=req.model,
        resume=req.resume,
        start=req.start,
        delay_seconds=(
            req.delay_seconds if req.delay_seconds is not None else DEFAULT_DELAY_SECONDS
        ),
        start_at=req.start_at,
        message=req.message,
    )


def due_after(stopped: datetime, delay_seconds: int) -> str:
    """The stored start time for a stop at ``stopped`` plus the delay."""
    return format_iso(stopped.astimezone(UTC) + timedelta(seconds=delay_seconds))
