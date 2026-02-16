"""Agent state tracking — push + pull reconciliation.

Push: Claude Code hooks write ~/.claude/state/{session}.json (external).
Pull: tmux capture-pane heuristics for ground truth.
Reconciliation: push preferred when fresh, pull overrides when stale.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from src.tmux import capture_pane

log = logging.getLogger(__name__)


class AgentState(StrEnum):
    """Possible agent states."""

    IDLE = "idle"
    STARTING = "starting"
    PROCESSING_ISSUE = "processing_issue"
    BUSY = "busy"
    PLAN_WAITING = "plan_waiting"
    UNKNOWN = "unknown"


@dataclass
class StateSnapshot:
    """A point-in-time agent state reading."""

    state: AgentState
    current_issue: int | None = None
    timestamp: float = 0.0
    source: str = "default"
    started_at: float | None = None
    plan_file: str | None = None
    plan_title: str | None = None


def read_state_file(state_dir: Path, session: str) -> StateSnapshot | None:
    """Read push-based state from ~/.claude/state/{session}.json.

    Expected JSON shape:
        {"state": "idle|processing_issue|busy", "issue": 42, "ts": 1234567890.0}

    Returns None if file doesn't exist or is unparseable.
    """
    state_file = state_dir / f"{session}.json"
    if not state_file.exists():
        return None
    try:
        data = json.loads(state_file.read_text())
        state_str = data.get("state", "unknown")
        try:
            state = AgentState(state_str)
        except ValueError:
            state = AgentState.UNKNOWN
        started_at_raw = data.get("started_at")
        return StateSnapshot(
            state=state,
            current_issue=data.get("issue"),
            timestamp=float(data.get("ts", 0)),
            source="push",
            started_at=float(started_at_raw) if started_at_raw is not None else None,
            plan_file=data.get("plan_file"),
            plan_title=data.get("plan_title"),
        )
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to read state file for %s: %s", session, e)
        return None


def infer_state_from_pane(pane_content: str) -> StateSnapshot:
    """Infer agent state from tmux capture-pane output.

    Heuristics:
    - Contains "Thinking..." or active tool calls → busy
    - Contains "working on issue #N" → processing_issue
    - Prompt visible (ends with $ or >) with no activity → idle
    - Otherwise → unknown
    """
    lines = pane_content.strip().splitlines()
    if not lines:
        return StateSnapshot(state=AgentState.UNKNOWN, source="pull")

    content = pane_content.lower()

    # Active processing indicators
    if "thinking..." in content or "tool call" in content:
        return StateSnapshot(state=AgentState.BUSY, source="pull")

    # Issue processing — look for issue references
    for line in reversed(lines):
        stripped = line.strip().lower()
        if "working on issue #" in stripped or "processing issue #" in stripped:
            # Try to extract issue number
            for word in stripped.split("#"):
                if word and word.split()[0].isdigit():
                    issue_num = int(word.split()[0])
                    return StateSnapshot(
                        state=AgentState.PROCESSING_ISSUE,
                        current_issue=issue_num,
                        source="pull",
                    )
            return StateSnapshot(state=AgentState.PROCESSING_ISSUE, source="pull")

    # Idle — scan last N non-empty lines for prompt characters.
    # Claude Code renders a status bar below the prompt, so the prompt
    # may not be the very last line.  Box-drawing separator lines (─)
    # are skipped — they are UI chrome, not content.
    _PROMPT_CHARS = ("$", ">", "\u276f", "%")  # ❯ = U+276F
    _BOX_CHARS = "\u2500\u2502\u250c\u2510\u2514\u2518\u252c\u2534\u253c\u2501"
    non_empty = [ln.strip() for ln in lines if ln.strip()]
    tail = non_empty[-5:]

    # Discard status bar: lines below the last separator are UI chrome.
    last_sep = -1
    for i, ln in enumerate(tail):
        if ln and all(ch in _BOX_CHARS for ch in ln):
            last_sep = i
    scan = tail[: last_sep + 1] if last_sep >= 0 else tail

    for candidate in reversed(scan):
        if candidate and all(ch in _BOX_CHARS for ch in candidate):
            continue
        if any(candidate.endswith(ch) for ch in _PROMPT_CHARS):
            return StateSnapshot(state=AgentState.IDLE, source="pull")
        break

    return StateSnapshot(state=AgentState.UNKNOWN, source="pull")


async def get_agent_state(
    state_dir: Path, session: str, stale_threshold: float = 300.0
) -> StateSnapshot:
    """Get reconciled agent state from push + pull sources.

    Push (state file) is preferred when fresh.
    Pull (tmux capture) overrides when push is stale or missing.
    """
    push = read_state_file(state_dir, session)

    # If push data exists and is fresh, use it
    if push and (time.time() - push.timestamp) < stale_threshold:
        return push

    # Trust stale idle — idle doesn't expire like busy does.
    # An idle agent stays idle until external input (which hooks capture).
    if push and push.state == AgentState.IDLE:
        return push

    # Pull from tmux (only for non-idle stale states)
    pane_content = await capture_pane(session)
    if pane_content:
        pull = infer_state_from_pane(pane_content)
        pull.timestamp = time.time()
        return pull

    # No pull data — use stale push or default to unknown
    if push:
        log.info(
            "Using stale push state for %s (age: %.0fs)", session, time.time() - push.timestamp
        )
        return push

    return StateSnapshot(state=AgentState.UNKNOWN, source="default")


def find_outgoing_comment(
    issue_number: int,
    action_log: str = "~/.claude/state/github-actions.jsonl",
    max_lines: int = 50,
    recency_seconds: float = 30.0,
) -> str | None:
    """Check if a comment on this issue was recently made by one of our agents.

    Reads the tail of the JSONL action log written by PostToolUse hooks.
    Matches by issue number + recency window (no comment_id in the log).
    Returns the originating session name, or None if not found.
    Graceful: returns None if the log file doesn't exist (hooks not yet set up).

    Log format: {"ts": 1234567890.0, "session": "ike", "action": "comment", "issue": 42}
    """
    log_path = Path(action_log).expanduser()
    if not log_path.exists():
        return None
    try:
        lines = log_path.read_text().strip().splitlines()
        now = time.time()
        # Only check the tail, most recent first
        for line in reversed(lines[-max_lines:]):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("action") == "comment" and entry.get("issue") == issue_number:
                ts = entry.get("ts", 0)
                if now - ts <= recency_seconds:
                    return entry.get("session")
        return None
    except OSError:
        return None


def has_commented_on_issue(
    issue_number: int,
    session: str,
    action_log: str = "~/.claude/state/github-actions.jsonl",
    max_lines: int = 200,
) -> bool:
    """Check if a session has ever commented on this issue (per action log).

    Unlike find_outgoing_comment(), this has NO recency window.
    Any comment at any time counts as acknowledgment.
    """
    log_path = Path(action_log).expanduser()
    if not log_path.exists():
        return False
    try:
        lines = log_path.read_text().strip().splitlines()
        for line in reversed(lines[-max_lines:]):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                entry.get("action") == "comment"
                and entry.get("issue") == issue_number
                and entry.get("session") == session
            ):
                return True
        return False
    except OSError:
        return False


def should_deliver(
    state: AgentState,
    is_blocking: bool = False,
    busy_duration: float | None = None,
    busy_threshold: float = 1800.0,
    require_idle: bool = False,
) -> bool:
    """Decide whether to deliver a notification based on agent state.

    Default (dispatcher) mode — permissive:
    - idle / starting / unknown: always deliver
    - processing_issue: deliver only if blocking
    - busy: defer unless duration >= threshold AND blocking (capacity routing)

    Monitor mode (require_idle=True) — strict:
    - Only deliver when state is confirmed idle.
    - Prevents startup floods and respects agent processing cycles.
    """
    if require_idle:
        return state == AgentState.IDLE
    if state in (AgentState.IDLE, AgentState.STARTING, AgentState.UNKNOWN):
        return True
    if state == AgentState.PROCESSING_ISSUE:
        return is_blocking
    # plan_waiting — agent is blocked waiting for human approval, never deliver
    if state == AgentState.PLAN_WAITING:
        return False
    # busy state — capacity-aware routing
    if state == AgentState.BUSY:
        if busy_duration is not None and busy_duration >= busy_threshold and is_blocking:
            return True
        return False
    return False
