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
_ANSI_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")
_PROMPT_CHARS = ("$", ">", "\u276f", "\u203a", "%")  # ❯, ›
_BOX_CHARS = "\u2500\u2502\u250c\u2510\u2514\u2518\u252c\u2534\u253c\u2501"


def _sanitize_pane_content(pane_content: str) -> str:
    """Strip terminal formatting so prompt/state heuristics see plain text."""
    return _ANSI_ESCAPE_RE.sub("", pane_content).replace("\xa0", " ")


def _prompt_tail_lines(pane_content: str) -> list[str]:
    """Relevant visible tail lines for prompt detection, excluding UI chrome."""
    lines = _sanitize_pane_content(pane_content).strip().splitlines()
    non_empty = [ln.rstrip() for ln in lines if ln.strip()]
    tail = non_empty[-5:]

    last_sep = -1
    for i, ln in enumerate(tail):
        stripped = ln.strip()
        if stripped and all(ch in _BOX_CHARS for ch in stripped):
            last_sep = i

    return tail[: last_sep + 1] if last_sep >= 0 else tail


def _prompt_tail_line_pairs(pane_content: str) -> list[tuple[str, str]]:
    """Tail raw/sanitized line pairs used for prompt detection."""
    raw_lines = pane_content.strip().splitlines()
    sanitized_lines = _sanitize_pane_content(pane_content).strip().splitlines()

    pairs = [
        (raw.rstrip(), sanitized.rstrip())
        for raw, sanitized in zip(raw_lines, sanitized_lines, strict=False)
        if sanitized.strip()
    ]
    tail = pairs[-5:]

    last_sep = -1
    for i, (_, sanitized) in enumerate(tail):
        stripped = sanitized.strip()
        if stripped and all(ch in _BOX_CHARS for ch in stripped):
            last_sep = i

    return tail[: last_sep + 1] if last_sep >= 0 else tail


def _is_status_chrome_line(line: str) -> bool:
    """Whether a visible tail line is session UI chrome rather than pane content."""
    stripped = line.strip().lower()
    if not stripped:
        return False
    if stripped.startswith("gpt-") and "·" in stripped:
        return True
    has_status_path = "~/" in stripped or "/users/" in stripped
    if "·" in stripped and (" left " in f" {stripped} " or has_status_path):
        return True
    return False


def _extract_prompt_line(pane_content: str) -> str | None:
    """Return the current prompt line if the visible tail shows one."""
    for _, candidate in reversed(_prompt_tail_line_pairs(pane_content)):
        stripped = candidate.strip()
        if not stripped:
            continue
        if all(ch in _BOX_CHARS for ch in stripped):
            continue
        if _is_status_chrome_line(stripped):
            continue
        if any(stripped.endswith(ch) for ch in _PROMPT_CHARS):
            return stripped
        if stripped[0] in _PROMPT_CHARS:
            return stripped
        break
    return None


def _extract_raw_prompt_line(pane_content: str) -> str | None:
    """Return the raw current prompt line if the visible tail shows one."""
    for raw_candidate, candidate in reversed(_prompt_tail_line_pairs(pane_content)):
        stripped = candidate.strip()
        if not stripped:
            continue
        if all(ch in _BOX_CHARS for ch in stripped):
            continue
        if _is_status_chrome_line(stripped):
            continue
        if any(stripped.endswith(ch) for ch in _PROMPT_CHARS):
            return raw_candidate.strip()
        if stripped[0] in _PROMPT_CHARS:
            return raw_candidate.strip()
        break
    return None


def _prompt_line_is_dim_placeholder(raw_prompt_line: str) -> bool:
    """Codex placeholder text is fully rendered as dim UI chrome, not input."""
    prompt_seen = False
    dim = False
    visible_tail_seen = False
    non_dim_tail_seen = False
    i = 0

    while i < len(raw_prompt_line):
        if raw_prompt_line[i] == "\x1b":
            match = _ANSI_SGR_RE.match(raw_prompt_line, i)
            if match:
                params = match.group(1)
                codes = [0] if params == "" else [int(part or 0) for part in params.split(";")]
                for code in codes:
                    if code == 0:
                        dim = False
                    elif code == 2:
                        dim = True
                    elif code == 22:
                        dim = False
                i = match.end()
                continue

        ch = raw_prompt_line[i]
        if not prompt_seen:
            if ch in _PROMPT_CHARS:
                prompt_seen = True
            i += 1
            continue

        if ch == "\n":
            break
        if ch.isspace():
            i += 1
            continue

        visible_tail_seen = True
        if not dim:
            non_dim_tail_seen = True
        i += 1

    return prompt_seen and visible_tail_seen and not non_dim_tail_seen


def prompt_has_pending_input(pane_content: str) -> bool:
    """Whether the current prompt line contains non-empty buffered input."""
    prompt_line = _extract_prompt_line(pane_content)
    if not prompt_line:
        return False
    if any(prompt_line.endswith(ch) for ch in _PROMPT_CHARS):
        return False
    raw_prompt_line = _extract_raw_prompt_line(pane_content)
    if raw_prompt_line and _prompt_line_is_dim_placeholder(raw_prompt_line):
        return False
    return len(prompt_line) > 1 and prompt_line[0] in _PROMPT_CHARS


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

    # Idle prompt at the bottom wins over stale activity markers higher up in
    # the pane. We only treat the session as busy when the current visible tail
    # does not show a ready prompt.
    non_empty = [ln.strip().lower() for ln in lines if ln.strip()]
    prompt_line = _extract_prompt_line(sanitized)
    if prompt_line:
        return StateSnapshot(state=AgentState.IDLE, source="pull")

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
