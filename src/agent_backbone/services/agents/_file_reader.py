"""The hook state files: ``<state_dir>/<agent>.json`` and ``<state_dir>/plans/``.

Runtime hooks write them; the backbone reads them (and writes one on behalf
of runtimes without hooks, through ``POST /api/agents/{name}/state``). The
``starting`` state has its own file, ``<agent>.starting``, written by
``start_agent`` and never by a hook, so launch bookkeeping and hook writes
cannot race on one path.
"""

from __future__ import annotations

import json
import logging
import math
import re
from pathlib import Path

from agent_backbone.fs import atomic_write_text
from agent_backbone.hooks.backbone_state import launch_state_path
from agent_backbone.services.agents.models import AgentState, StateSnapshot
from agent_backbone.services.runtimes import RuntimeDiagnostic

log = logging.getLogger(__name__)

_ERROR_NAME = re.compile(r"[A-Za-z0-9_.-]{1,80}")
_MODEL_ID = re.compile(r"[A-Za-z0-9_./:@+-]{1,160}")


def write_state_file(state_dir: Path, session: str, record: dict) -> Path:
    """Write ``<state_dir>/<session>.json`` in the hook's own shape."""
    target = state_dir / f"{session}.json"
    if scoped := launch_state_path(state_dir, session, record.get("launch_id")):
        atomic_write_text(scoped, json.dumps(record))
    atomic_write_text(target, json.dumps(record))
    return target


def _marker_path(state_dir: Path, session: str) -> Path:
    return state_dir / f"{session}.starting"


def read_launch_marker(state_dir: Path, session: str) -> tuple[float, str] | None:
    """The latest managed launch, retained after its temporary starting marker clears."""
    try:
        record = json.loads((state_dir / f"{session}.launch").read_text())
        timestamp = float(record["ts"])
        launch_id = record["launch_id"]
        if math.isfinite(timestamp) and isinstance(launch_id, str) and launch_id:
            return timestamp, launch_id
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def write_starting_marker(
    state_dir: Path,
    session: str,
    launched_at: float,
    *,
    launch_id: str | None = None,
    opened: tuple[str, str] | None = None,
) -> None:
    """Record that ``session`` was launched at ``launched_at`` and is not at its prompt yet.

    ``opened`` is ``(runtime, session id)`` when the launch resumed that exact
    conversation: it is the launch's conversation before its hook reports."""
    if launch_id:
        record = {"ts": launched_at, "launch_id": launch_id}
        if opened is not None:
            record.update(runtime=opened[0], session_id=opened[1])
        atomic_write_text(state_dir / f"{session}.launch", json.dumps(record))
    atomic_write_text(_marker_path(state_dir, session), json.dumps({"ts": launched_at}))


def read_launch_session(state_dir: Path, session: str, runtime: str) -> str | None:
    """The conversation the latest launch resumed by id on ``runtime``, if it did."""
    try:
        record = json.loads((state_dir / f"{session}.launch").read_text())
    except (OSError, ValueError):
        return None
    session_id = record.get("session_id") if isinstance(record, dict) else None
    if record.get("runtime") == runtime and isinstance(session_id, str) and session_id:
        return session_id
    return None


def clear_starting_marker(state_dir: Path, session: str) -> None:
    """The launch is over (prompt seen, or the session died): forget the marker."""
    _marker_path(state_dir, session).unlink(missing_ok=True)


def _starting_snapshot(state_dir: Path, session: str, newer_than: float) -> StateSnapshot | None:
    """``starting`` from the marker, unless hook state newer than the marker exists."""
    marker = _marker_path(state_dir, session)
    try:
        launched_at = float(json.loads(marker.read_text())["ts"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not math.isfinite(launched_at):
        return None  # an "inf" marker would outrank every later hook state
    if launched_at <= newer_than:
        clear_starting_marker(state_dir, session)  # a hook has spoken since the launch
        return None
    return StateSnapshot(
        state=AgentState.STARTING,
        timestamp=launched_at,
        source="push",
        started_at=launched_at,
        evidence=[f"launch marker {marker.name}: starting"],
    )


def _finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _hook_diagnostics(data: dict) -> tuple[RuntimeDiagnostic, ...]:
    """What a hook recorded about the runtime's replies, for runtimes whose screen does not show it.

    ``request_error`` is ``{"name": ..., "status": 400-599}`` (status optional),
    ``model_changed`` a ``provider/model`` identifier. The file is data: a
    value of any other shape is ignored.
    """
    found = []
    error = data.get("request_error")
    if isinstance(error, dict) and isinstance(error.get("name"), str):
        status = error.get("status")
        if _ERROR_NAME.fullmatch(error["name"]) and (
            status is None or (type(status) is int and 400 <= status <= 599)
        ):
            found.append(
                RuntimeDiagnostic(
                    code="request_error", error_type=error["name"], http_status=status
                )
            )
    model = data.get("model_changed")
    if isinstance(model, str) and _MODEL_ID.fullmatch(model):
        found.append(RuntimeDiagnostic(code="model_changed", severity="info", model=model))
    return tuple(found)


def _read_record(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        if _finite(data.get("ts", 0)) is None or (
            data.get("started_at") is not None and _finite(data["started_at"]) is None
        ):
            raise ValueError("non-finite timestamp")
        return data
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError) as exc:
        log.warning("Failed to read state file %s: %s", path.name, exc)
        return None


def _belongs_to_launch(data: dict, launch: tuple[float, str] | None) -> bool:
    return launch is None or (
        float(data.get("ts", 0)) >= launch[0] and data.get("launch_id") in (None, launch[1])
    )


def read_state_file(
    state_dir: Path, session: str, *, current_launch: bool = False
) -> StateSnapshot | None:
    """Read hook-written state from ``<state_dir>/<session>.json``.

    Expected JSON shape::

        {"state": "idle|busy|waiting_for_human|starting|unknown", "reason": "plan",
         "issue": 42, "repo": "owner/name", "ts": 1234567890.0,
         "started_at": 1234567800.0, "plan_file": "...", "plan_title": "..."}

    Returns None if the file does not exist or cannot be parsed. State decisions
    request ``current_launch``; historical readers retain the saved conversation
    for usage accounting, transcripts and explicit resume.
    """
    launch = read_launch_marker(state_dir, session)
    state_file = state_dir / f"{session}.json"
    data = _read_record(state_file)
    if launch is not None:
        scoped = launch_state_path(state_dir, session, launch[1])
        current = _read_record(scoped) if scoped is not None else None
        if (
            current is not None
            and _belongs_to_launch(current, launch)
            and (
                data is None
                or not _belongs_to_launch(data, launch)
                or float(current.get("ts", 0)) >= float(data.get("ts", 0))
            )
        ):
            state_file, data = scoped, current
    if data is None:
        return _starting_snapshot(state_dir, session, newer_than=0.0)
    hook_ts = float(data.get("ts", 0))
    started_at = _finite(data.get("started_at"))
    belongs = _belongs_to_launch(data, launch)
    starting = _starting_snapshot(state_dir, session, newer_than=hook_ts if belongs else 0.0)
    if starting is not None:
        return starting
    if current_launch and not belongs:
        return None
    state = AgentState.parse(data.get("state"))
    return StateSnapshot(
        state=state,
        reason=data.get("reason") or None,
        current_issue=data.get("issue"),
        current_repo=data.get("repo") or None,
        timestamp=hook_ts,
        source="push",
        started_at=started_at,
        plan_file=data.get("plan_file"),
        plan_title=data.get("plan_title"),
        session_id=data.get("session_id") or None,
        runtime=data.get("runtime") or None,
        model=data["model"] if isinstance(data.get("model"), str) and data["model"] else None,
        last_message=data.get("last_message") or None,
        event=data.get("event") or None,
        prompted_at=_finite(data.get("prompted_at")),
        prompt_digest=data["prompt_digest"] if isinstance(data.get("prompt_digest"), str) else None,
        detail=data.get("detail") or None,
        diagnostics=_hook_diagnostics(data),
        evidence=[
            f"hook state file {state_file.name}: {state.value}"
            + (f" (event {data['event']})" if data.get("event") else "")
        ],
    )


def confined_plan_path(state_dir: Path, plan_file: str) -> Path | None:
    """``plan_file`` resolved, only if it lives under ``<state_dir>/plans``.

    The hook writes plans there; the recorded path is still data from a state
    file (or a ``POST /api/agents/{name}/state`` body), so it is never trusted
    to point anywhere else on the machine.
    """
    plans_dir = (state_dir / "plans").resolve()
    path = Path(plan_file).expanduser().resolve()
    if not path.is_relative_to(plans_dir):
        log.warning("Refusing to read plan outside %s: %s", plans_dir, plan_file)
        return None
    return path


def read_plan(state_dir: Path, snapshot: StateSnapshot) -> str | None:
    """The text of the plan a snapshot points at, or None when there is none to show."""
    if not snapshot.plan_file:
        return None
    path = confined_plan_path(state_dir, snapshot.plan_file)
    if path is None or not path.is_file():
        return None
    try:
        return path.read_text()
    except OSError:
        return None
