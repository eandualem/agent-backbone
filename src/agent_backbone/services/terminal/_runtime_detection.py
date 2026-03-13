"""Pure helpers for terminal runtime and prompt detection."""

from __future__ import annotations

import re

from agent_backbone.services.terminal._runtime_types import TerminalRuntime

_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_ANSI_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")
BOX_CHARS = "\u2500\u2502\u250c\u2510\u2514\u2518\u252c\u2534\u253c\u2501"
_PROMPT_START_CHARS = ">$\u276f\u203a%#"


def sanitize_pane_content(pane_content: str) -> str:
    """Strip ANSI escape sequences for prompt/runtime analysis."""
    return _ANSI_ESCAPE_RE.sub("", pane_content).replace("\xa0", " ")


def prompt_tail_line_pairs(pane_content: str) -> list[tuple[str, str]]:
    """Tail raw/sanitized line pairs used for prompt detection."""
    raw_lines = pane_content.strip().splitlines()
    sanitized_lines = sanitize_pane_content(pane_content).strip().splitlines()

    pairs = [
        (raw.rstrip(), sanitized.rstrip())
        for raw, sanitized in zip(raw_lines, sanitized_lines, strict=False)
        if sanitized.strip()
    ]
    tail = pairs[-8:]

    last_sep = -1
    for i, (_, sanitized) in enumerate(tail):
        stripped = sanitized.strip()
        if stripped and all(ch in BOX_CHARS for ch in stripped):
            last_sep = i

    return tail[: last_sep + 1] if last_sep >= 0 else tail


def prompt_line_is_dim_placeholder(raw_prompt_line: str) -> bool:
    """Whether the visible prompt tail is purely dim placeholder chrome."""
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
            if ch in ">$\u276f\u203a%":
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


def runtime_from_prompt_line(pane_content: str) -> TerminalRuntime:
    """Infer runtime from a bare prompt line when no richer markers are visible."""
    for _, candidate in reversed(prompt_tail_line_pairs(pane_content)):
        stripped = candidate.strip()
        if not stripped:
            continue
        if all(ch in BOX_CHARS for ch in stripped):
            continue
        if stripped.startswith("\u276f"):
            return TerminalRuntime.CLAUDE
        if stripped.startswith("\u203a"):
            return TerminalRuntime.CODEX
        break
    return TerminalRuntime.UNKNOWN


def runtime_analysis_line_pairs(pane_content: str) -> list[tuple[str, str]]:
    """Narrow runtime matching to the active prompt region near the pane tail."""
    pairs = prompt_tail_line_pairs(pane_content)
    if not pairs:
        return []

    prompt_idx: int | None = None
    for i in range(len(pairs) - 1, -1, -1):
        stripped = pairs[i][1].strip()
        if not stripped or all(ch in BOX_CHARS for ch in stripped):
            continue
        if stripped[0] in _PROMPT_START_CHARS:
            prompt_idx = i
            break

    if prompt_idx is None:
        return pairs[-6:]

    return pairs[prompt_idx : min(len(pairs), prompt_idx + 3)]


def runtime_analysis_text(pane_content: str) -> str:
    """Sanitized active-tail text used for runtime marker detection."""
    return "\n".join(
        sanitized.strip().lower()
        for _, sanitized in runtime_analysis_line_pairs(pane_content)
        if sanitized.strip()
    )
