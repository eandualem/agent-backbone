"""Pane text analysis shared by every runtime: ANSI stripping, the prompt tail,
dim-placeholder detection. Pure functions over captured terminal text."""

from __future__ import annotations

import re

_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_ANSI_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")
BOX_CHARS = "─│┌┐└┘┬┴┼━"
PROMPT_START_CHARS = ">$❯›%#"
ENVELOPE_PREFIX = "[via:"
GENERIC_BUSY_FRAGMENTS = ("thinking...", "tool call")
"""Last-resort busy evidence for panes no runtime recognises (a bare
``Thinking...`` line, a tool-call trace) — consulted only when no prompt,
busy marker or question was found."""

# A dialog's own option lines: "❯ 1. Yes", "› 1. Yes, proceed (y)", "● 1. Yes", "  2. No".
DIALOG_OPTION_RE = re.compile(r"^\S{0,2}\s*\d{1,2}\.\s")


def sanitize_pane_content(pane_content: str) -> str:
    """Strip ANSI escape sequences for prompt/runtime analysis."""
    return _ANSI_ESCAPE_RE.sub("", pane_content).replace("\xa0", " ")


def is_box_line(stripped: str) -> bool:
    return bool(stripped) and all(ch in BOX_CHARS for ch in stripped)


def prompt_tail_line_pairs(pane_content: str) -> list[tuple[str, str]]:
    """Tail raw/sanitized line pairs used for prompt detection."""
    # Sanitize each raw line on its own so the pairing is structural: a line
    # that is only escape sequences vanishes from the sanitized text and
    # would otherwise shift every pair after it.
    pairs: list[tuple[str, str]] = []
    for raw in pane_content.strip().splitlines():
        sanitized = sanitize_pane_content(raw)
        if sanitized.strip():
            pairs.append((raw.rstrip(), sanitized.rstrip()))
    tail = pairs[-8:]

    last_sep = -1
    for i, (_, sanitized) in enumerate(tail):
        if is_box_line(sanitized.strip()):
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
            other = _ANSI_ESCAPE_RE.match(raw_prompt_line, i)
            if other:  # a cursor/erase sequence, not text
                i = other.end()
                continue

        ch = raw_prompt_line[i]
        if not prompt_seen:
            if ch in ">$❯›%":
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


def runtime_analysis_text(pane_content: str) -> str:
    """Sanitized active-tail text used for runtime marker detection."""
    pairs = prompt_tail_line_pairs(pane_content)
    if not pairs:
        return ""

    prompt_idx: int | None = None
    for i in range(len(pairs) - 1, -1, -1):
        stripped = pairs[i][1].strip()
        if not stripped or is_box_line(stripped):
            continue
        if stripped[0] in PROMPT_START_CHARS:
            prompt_idx = i
            break

    window = (
        pairs[-6:] if prompt_idx is None else pairs[prompt_idx : min(len(pairs), prompt_idx + 3)]
    )
    return "\n".join(sanitized.strip().lower() for _, sanitized in window if sanitized.strip())


def last_prompt_char(pane_content: str) -> str | None:
    """The first character of the last non-chrome line, if it looks like a prompt."""
    for _, candidate in reversed(prompt_tail_line_pairs(pane_content)):
        stripped = candidate.strip()
        if not stripped or is_box_line(stripped):
            continue
        return stripped[0]
    return None
