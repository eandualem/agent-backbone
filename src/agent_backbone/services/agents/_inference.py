"""Pull-based state inference from tmux + push/pull reconciliation."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from agent_backbone.services.agents._file_reader import read_state_file
from agent_backbone.services.agents.models import AgentState, StateSnapshot
from agent_backbone.services.terminal import capture_pane

log = logging.getLogger(__name__)

_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _sanitize_pane_content(pane_content: str) -> str:
    """Strip terminal formatting so prompt/state heuristics see plain text."""
    return _ANSI_ESCAPE_RE.sub("", pane_content).replace("\xa0", " ")


def infer_state_from_pane(pane_content: str) -> StateSnapshot:
    """Infer agent state from tmux capture-pane output.

    Heuristics:
    - Contains "Thinking..." or active tool calls -> busy
    - Contains "working on issue #N" -> processing_issue
    - Prompt visible (ends with $ or >) with no activity -> idle
    - Otherwise -> unknown
    """
    sanitized = _sanitize_pane_content(pane_content)
    lines = sanitized.strip().splitlines()
    if not lines:
        return StateSnapshot(state=AgentState.UNKNOWN, source="pull")

    content = sanitized.lower()

    # Idle prompt at the bottom wins over stale activity markers higher up in
    # the pane. We only treat the session as busy when the current visible tail
    # does not show a ready prompt.
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

    recent_lines = [ln.strip().lower() for ln in non_empty[-20:]]
    recent_content = "\n".join(recent_lines)

    # Active processing indicators
    if "thinking..." in recent_content or "tool call" in recent_content:
        return StateSnapshot(state=AgentState.BUSY, source="pull")

    # Issue processing — look for issue references in the visible tail.
    for stripped in reversed(recent_lines):
        if "working on issue #" in stripped or "processing issue #" in stripped:
            for word in stripped.split("#"):
                if word and word.split()[0].isdigit():
                    issue_num = int(word.split()[0])
                    return StateSnapshot(
                        state=AgentState.PROCESSING_ISSUE,
                        current_issue=issue_num,
                        source="pull",
                    )
            return StateSnapshot(state=AgentState.PROCESSING_ISSUE, source="pull")

    return StateSnapshot(state=AgentState.UNKNOWN, source="pull")


async def get_agent_state(
    state_dir: Path, session: str, stale_threshold: float = 300.0
) -> StateSnapshot:
    """Get reconciled agent state from push + pull sources.

    Push (state file) is preferred when fresh.
    Stale or missing push state is actively verified from tmux before any
    stale fallback is trusted.
    """
    push = read_state_file(state_dir, session)

    # If push data exists and is fresh, use it
    if push and (time.time() - push.timestamp) < stale_threshold:
        return push

    # Verify stale or missing state from tmux before trusting old push data.
    pane_content = await capture_pane(session)
    if pane_content:
        pull = infer_state_from_pane(pane_content)
        pull.timestamp = time.time()
        return pull

    # No tmux evidence available — fall back to stale push if present.
    if push:
        log.info(
            "Using stale push state for %s (age: %.0fs)", session, time.time() - push.timestamp
        )
        return push

    return StateSnapshot(state=AgentState.UNKNOWN, source="default")
