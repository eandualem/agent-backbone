"""The hook state files: ``<state_dir>/<agent>.json`` and ``<state_dir>/plans/``.

Runtime hooks write them; the backbone reads them (and writes one on behalf
of runtimes without hooks, through ``POST /api/agents/{name}/state``).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from agent_backbone.services.agents.models import AgentState, StateSnapshot

log = logging.getLogger(__name__)


def write_state_file(state_dir: Path, session: str, record: dict) -> Path:
    """Atomically write ``<state_dir>/<session>.json`` in the hook's own shape."""
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / f"{session}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record))
    os.replace(tmp, target)
    return target


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
        return None
    try:
        data = json.loads(state_file.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to read state file for %s: %s", session, e)
        return None

    state = AgentState.parse(data.get("state"))
    started_at_raw = data.get("started_at")
    return StateSnapshot(
        state=state,
        reason=data.get("reason") or None,
        current_issue=data.get("issue"),
        current_repo=data.get("repo") or None,
        timestamp=float(data.get("ts", 0)),
        source="push",
        started_at=float(started_at_raw) if started_at_raw is not None else None,
        plan_file=data.get("plan_file"),
        plan_title=data.get("plan_title"),
        evidence=[f"hook state file {state_file.name}: {state.value}"],
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
