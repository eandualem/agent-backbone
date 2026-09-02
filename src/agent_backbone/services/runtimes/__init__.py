"""Runtimes — one object per interactive CLI the backbone can run.

``RUNTIMES`` is the registry (id → ``Runtime``); ``get_runtime`` resolves a
name or alias, ``detect_runtime`` reads a pane, ``resolve_runtime`` asks a
live session (hint, then its environment, then the pane). ``send_message``
pastes through the resolved runtime. Everything runtime-specific lives in
the modules next to this file; nothing else in the backbone names a CLI.
"""

from __future__ import annotations

from agent_backbone.config import RUNTIMES as RUNTIME_IDS
from agent_backbone.services.runtimes import (
    aider,
    claude,
    codex,
    deepcode,
    gemini,
    opencode,
    shell,
)
from agent_backbone.services.runtimes._pane import GENERIC_BUSY_FRAGMENTS, sanitize_pane_content
from agent_backbone.services.runtimes.base import Runtime, read_brief, resolve_command
from agent_backbone.services.terminal import capture_pane, query_environment_var

RUNTIME_ENV_KEY = "BACKBONE_RUNTIME"
AGENT_ENV_KEY = "BACKBONE_AGENT"
STATE_DIR_ENV_KEY = "BACKBONE_STATE_DIR"

RUNTIMES: dict[str, Runtime] = {
    r.id: r
    for r in (
        claude.RUNTIME,
        codex.RUNTIME,
        gemini.RUNTIME,
        opencode.RUNTIME,
        deepcode.RUNTIME,
        aider.RUNTIME,
        shell.RUNTIME,
    )
}
"""Every runtime an agent can be started with, in display order."""
UNKNOWN = shell.UNKNOWN

if set(RUNTIMES) != set(RUNTIME_IDS):
    raise RuntimeError(
        "config.RUNTIMES must list the registered runtimes: "
        f"{sorted(set(RUNTIMES) ^ set(RUNTIME_IDS))}"
    )
_ALIASES: dict[str, Runtime] = {alias: r for r in RUNTIMES.values() for alias in r.aliases}

_DETECTION_ORDER = (
    deepcode.RUNTIME,
    gemini.RUNTIME,
    opencode.RUNTIME,
    aider.RUNTIME,
    codex.RUNTIME,
    claude.RUNTIME,
)
"""Runtimes with distinctive markers first; Claude's markers are the most generic."""


def get_runtime(value: str | Runtime | None) -> Runtime:
    """The runtime for an id or alias; ``UNKNOWN`` when nothing matches."""
    if isinstance(value, Runtime):
        return value
    if not value:
        return UNKNOWN
    key = value.strip().lower()
    return RUNTIMES.get(key) or _ALIASES.get(key, UNKNOWN)


def detect_runtime(pane_content: str) -> Runtime:
    """Best-effort runtime detection from visible pane content."""
    if not pane_content.strip():
        return UNKNOWN
    for runtime in _DETECTION_ORDER:
        if runtime.matches(pane_content):
            return runtime
    from agent_backbone.services.runtimes._pane import last_prompt_char

    first = last_prompt_char(pane_content)
    if first == "❯":
        return claude.RUNTIME
    if first == "›":
        return codex.RUNTIME
    if shell.RUNTIME.detect_idle(pane_content):
        return shell.RUNTIME
    return UNKNOWN


async def resolve_runtime(
    session_name: str,
    *,
    hint: str | None = None,
    pane_content: str | None = None,
) -> Runtime:
    """The runtime of a live session: the hint, then ``$BACKBONE_RUNTIME``, then the pane."""
    hinted = get_runtime(hint)
    if hinted is not UNKNOWN:
        return hinted
    from_env = get_runtime(await query_environment_var(session_name, RUNTIME_ENV_KEY))
    if from_env is not UNKNOWN:
        return from_env
    if pane_content is None:
        pane_content = await capture_pane(session_name, lines=80)
    return detect_runtime(pane_content)


async def send_message(session_name: str, message: str, *, runtime_hint: str | None = None) -> bool:
    """Paste ``message`` into a session and submit it the way its runtime expects."""
    pane_content = await capture_pane(session_name, lines=80)
    runtime = await resolve_runtime(session_name, hint=runtime_hint, pane_content=pane_content)
    return await runtime.deliver_message(session_name, message)


__all__ = [
    "AGENT_ENV_KEY",
    "GENERIC_BUSY_FRAGMENTS",
    "RUNTIMES",
    "RUNTIME_ENV_KEY",
    "STATE_DIR_ENV_KEY",
    "UNKNOWN",
    "Runtime",
    "detect_runtime",
    "get_runtime",
    "read_brief",
    "resolve_command",
    "resolve_runtime",
    "sanitize_pane_content",
    "send_message",
]
