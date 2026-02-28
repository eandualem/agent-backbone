"""Pull-based state inference from tmux + push/pull reconciliation."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from agent_backbone.services.state._file_reader import read_state_file
from agent_backbone.services.state.models import AgentState, StateSnapshot
from agent_backbone.services.tmux import capture_pane

log = logging.getLogger(__name__)


def infer_state_from_pane(pane_content: str) -> StateSnapshot:
    """Infer agent state from tmux capture-pane output.

    Heuristics:
    - Contains "Thinking..." or active tool calls -> busy
    - Contains "working on issue #N" -> processing_issue
    - Prompt visible (ends with $ or >) with no activity -> idle
    - Otherwise -> unknown
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
