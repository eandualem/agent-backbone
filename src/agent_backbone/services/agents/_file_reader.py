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
from pathlib import Path

from agent_backbone.fs import atomic_write_text
from agent_backbone.services.agents.models import AgentState, StateSnapshot

log = logging.getLogger(__name__)


def write_state_file(state_dir: Path, session: str, record: dict) -> Path:
    """Write ``<state_dir>/<session>.json`` in the hook's own shape."""
    target = state_dir / f"{session}.json"
    atomic_write_text(target, json.dumps(record))
    return target


def _marker_path(state_dir: Path, session: str) -> Path:
    return state_dir / f"{session}.starting"


def write_starting_marker(state_dir: Path, session: str, launched_at: float) -> None:
    """Record that ``session`` was launched at ``launched_at`` and is not at its prompt yet."""
    atomic_write_text(_marker_path(state_dir, session), json.dumps({"ts": launched_at}))


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


def read_state_file(state_dir: Path, session: str) -> StateSnapshot | None:
    """Read hook-written state from ``<state_dir>/<session>.json``.

    Expected JSON shape::

        {"state": "idle|busy|waiting_for_human|starting|unknown", "reason": "plan",
         "issue": 42, "repo": "owner/name", "ts": 1234567890.0,
         "started_at": 1234567800.0, "plan_file": "...", "plan_title": "..."}

    Returns None if the file does not exist or cannot be parsed.
    """
    state_file = state_dir / f"{session}.json"
    if not state_file.exists():
        return _starting_snapshot(state_dir, session, newer_than=0.0)
    try:
        data = json.loads(state_file.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to read state file for %s: %s", session, e)
        return None

    hook_ts = float(data.get("ts", 0))
    starting = _starting_snapshot(state_dir, session, newer_than=hook_ts)
    if starting is not None:
        return starting
    state = AgentState.parse(data.get("state"))
    started_at_raw = data.get("started_at")
    return StateSnapshot(
        state=state,
        reason=data.get("reason") or None,
        current_issue=data.get("issue"),
        current_repo=data.get("repo") or None,
        timestamp=hook_ts,
        source="push",
        started_at=float(started_at_raw) if started_at_raw is not None else None,
        plan_file=data.get("plan_file"),
        plan_title=data.get("plan_title"),
        session_id=data.get("session_id") or None,
        last_message=data.get("last_message") or None,
        event=data.get("event") or None,
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
