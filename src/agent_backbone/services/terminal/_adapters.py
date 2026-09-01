"""CLI adapter layer for tmux-backed terminal sessions."""

from __future__ import annotations

import asyncio
import logging
import re
from enum import StrEnum

from agent_backbone.services.terminal._core import (
    _run_tmux,
    _send_escape_key,
    _send_submit_key,
    _write_message_buffer,
    capture_pane,
)
from agent_backbone.services.terminal._sessions import query_environment_var

log = logging.getLogger(__name__)

RUNTIME_ENV_KEY = "BACKBONE_RUNTIME"
AGENT_ENV_KEY = "BACKBONE_AGENT"
STATE_DIR_ENV_KEY = "BACKBONE_STATE_DIR"
_SUBMIT_RECHECK_DELAY_SECONDS = 0.1
_MAX_SUBMIT_ATTEMPTS = 2
_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_ANSI_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")
_BOX_CHARS = "\u2500\u2502\u250c\u2510\u2514\u2518\u252c\u2534\u253c\u2501"
_PROMPT_START_CHARS = ">$\u276f\u203a%#"
_BACKBONE_ENVELOPE_PREFIX = "[via:"


class TerminalRuntime(StrEnum):
    """Known interactive CLI runtimes."""

    CLAUDE = "claude"
    GEMINI = "gemini"
    CODEX = "codex"
    CURSOR = "cursor"
    OPENCODE = "opencode"
    AIDER = "aider"
    SHELL = "shell"
    UNKNOWN = "unknown"


def sanitize_pane_content(pane_content: str) -> str:
    """Strip ANSI escape sequences for prompt/runtime analysis."""
    return _ANSI_ESCAPE_RE.sub("", pane_content).replace("\xa0", " ")


def _prompt_tail_line_pairs(pane_content: str) -> list[tuple[str, str]]:
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
        if stripped and all(ch in _BOX_CHARS for ch in stripped):
            last_sep = i

    return tail[: last_sep + 1] if last_sep >= 0 else tail


def _prompt_line_is_dim_placeholder(raw_prompt_line: str) -> bool:
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


def _runtime_from_prompt_line(pane_content: str) -> TerminalRuntime:
    """Infer runtime from a bare prompt line when no richer markers are visible."""
    for _, candidate in reversed(_prompt_tail_line_pairs(pane_content)):
        stripped = candidate.strip()
        if not stripped:
            continue
        if all(ch in _BOX_CHARS for ch in stripped):
            continue
        if stripped.startswith("\u276f"):
            return TerminalRuntime.CLAUDE
        if stripped.startswith("\u203a"):
            return TerminalRuntime.CODEX
        break
    return TerminalRuntime.UNKNOWN


def _runtime_analysis_line_pairs(pane_content: str) -> list[tuple[str, str]]:
    """Narrow runtime matching to the active prompt region near the pane tail."""
    pairs = _prompt_tail_line_pairs(pane_content)
    if not pairs:
        return []

    prompt_idx: int | None = None
    for i in range(len(pairs) - 1, -1, -1):
        stripped = pairs[i][1].strip()
        if not stripped or all(ch in _BOX_CHARS for ch in stripped):
            continue
        if stripped[0] in _PROMPT_START_CHARS:
            prompt_idx = i
            break

    if prompt_idx is None:
        return pairs[-6:]

    return pairs[prompt_idx : min(len(pairs), prompt_idx + 3)]


def _runtime_analysis_text(pane_content: str) -> str:
    """Sanitized active-tail text used for runtime marker detection."""
    return "\n".join(
        sanitized.strip().lower()
        for _, sanitized in _runtime_analysis_line_pairs(pane_content)
        if sanitized.strip()
    )


def normalize_runtime(value: str | None) -> TerminalRuntime:
    """Normalize a free-form runtime label to a known runtime."""
    if not value:
        return TerminalRuntime.UNKNOWN
    normalized = value.strip().lower()
    aliases = {
        "claude-code": TerminalRuntime.CLAUDE,
        "claude code": TerminalRuntime.CLAUDE,
        "gemini-cli": TerminalRuntime.GEMINI,
        "open-code": TerminalRuntime.OPENCODE,
        "open_code": TerminalRuntime.OPENCODE,
        "aider-chat": TerminalRuntime.AIDER,
    }
    try:
        return TerminalRuntime(normalized)
    except ValueError:
        return aliases.get(normalized, TerminalRuntime.UNKNOWN)


class TerminalAdapter:
    """Behavioral contract for a single interactive CLI."""

    runtime: TerminalRuntime = TerminalRuntime.UNKNOWN
    prompt_prefixes: tuple[str, ...] = ()
    prompt_suffixes: tuple[str, ...] = ()
    runtime_markers: tuple[str, ...] = ()
    placeholder_fragments: tuple[str, ...] = ()
    status_fragments: tuple[str, ...] = ()
    queue_markers: tuple[str, ...] = ()
    busy_markers: tuple[str, ...] = ()
    """Fragments shown only while the runtime is working (e.g. a spinner line)."""
    prompt_markers: tuple[str, ...] = ()
    """Fragments shown when the runtime is asking the human a yes/no question."""
    auto_submit: bool = False
    submit_attempts: int = 1
    interrupt_queued_delivery: bool = False
    paste_settle_seconds: float = 0.0

    def detect_prompt(self, pane_content: str) -> str | None:
        """Return the visible prompt line when this adapter recognizes one."""
        for raw_candidate, candidate in reversed(_prompt_tail_line_pairs(pane_content)):
            stripped = candidate.strip()
            if not stripped:
                continue
            if all(ch in _BOX_CHARS for ch in stripped):
                continue
            if self._is_status_chrome_line(stripped):
                continue
            if self._matches_prompt_line(stripped):
                return raw_candidate.strip()
            break

        fallback = self._detect_idle_placeholder_line(pane_content)
        if fallback:
            return fallback
        return None

    def detect_busy(self, pane_content: str) -> bool:
        """Whether the runtime is visibly working (spinner / interrupt hint)."""
        if not self.busy_markers:
            return False
        tail = sanitize_pane_content(pane_content).strip().splitlines()[-12:]
        lowered = "\n".join(line.lower() for line in tail)
        return any(marker in lowered for marker in self.busy_markers)

    def detect_waiting_for_human(self, pane_content: str) -> bool:
        """Whether the runtime is visibly blocked on a question to the human."""
        if not self.prompt_markers:
            return False
        tail = sanitize_pane_content(pane_content).strip().splitlines()[-15:]
        lowered = "\n".join(line.lower() for line in tail)
        return any(marker in lowered for marker in self.prompt_markers)

    def detect_idle(self, pane_content: str) -> bool:
        """Whether the pane currently shows an interactive prompt surface."""
        if self.detect_busy(pane_content) or self.detect_waiting_for_human(pane_content):
            return False
        return self.detect_prompt(pane_content) is not None

    def prompt_has_pending_input(self, pane_content: str) -> bool:
        """Whether the prompt currently contains buffered user input."""
        prompt_line = self.detect_prompt(pane_content)
        if not prompt_line:
            return False

        sanitized = sanitize_pane_content(prompt_line).strip()
        lowered = sanitized.lower()
        if any(lowered.endswith(suffix) for suffix in self.prompt_suffixes):
            return False
        if any(lowered == prefix for prefix in self.prompt_prefixes):
            return False
        if any(fragment in lowered for fragment in self.placeholder_fragments):
            return False
        if _prompt_line_is_dim_placeholder(prompt_line):
            # Dim text after the prompt is a suggestion/placeholder, not typed input.
            return False

        # --- Guards against mistaking leftover output for typed input ---

        # Prefix guard: if the adapter defines prompt_prefixes and the sanitized
        # line doesn't start with any of them, we matched via a suffix — the
        # "pending text" is just trailing output, not user input.
        if self.prompt_prefixes and not any(
            sanitized.startswith(prefix) for prefix in self.prompt_prefixes
        ):
            return False

        # Stuck envelope: text after the prompt char that begins with a backbone
        # message envelope tag is a prior delivery that wasn't consumed, not user
        # input.  Strip the prompt prefix before checking.
        remainder = sanitized
        for prefix in self.prompt_prefixes:
            if sanitized.startswith(prefix):
                remainder = sanitized[len(prefix) :].lstrip()
                break
        return not remainder.startswith(_BACKBONE_ENVELOPE_PREFIX)

    async def exit_copy_mode(self, session_name: str) -> bool:
        """Immediately attempt to leave copy mode."""
        rc, _, stderr = await _run_tmux("send-keys", "-X", "-t", session_name, "cancel")
        if rc == 0:
            return True

        err = stderr.decode().strip().lower()
        if "not in a mode" in err:
            return True

        log.error("tmux copy-mode cancel failed for '%s': %s", session_name, stderr.decode())
        return False

    async def deliver_message(self, session_name: str, message: str) -> bool:
        """Paste a message and submit it according to runtime-specific rules."""
        if not await _write_message_buffer(session_name, message):
            return False

        if self.paste_settle_seconds > 0:
            await asyncio.sleep(self.paste_settle_seconds)

        if self.auto_submit:
            log.info(
                "Terminal delivery sent to '%s' via %s adapter (auto-submit)",
                session_name,
                self.runtime.value,
            )
            return True

        state = "submitted"
        for _attempt in range(self.submit_attempts):
            if not await _send_submit_key(session_name):
                return False

            await asyncio.sleep(_SUBMIT_RECHECK_DELAY_SECONDS)
            state = await self.delivery_submission_state(session_name)
            if state == "submitted":
                log.info(
                    "Terminal delivery sent to '%s' via %s adapter",
                    session_name,
                    self.runtime.value,
                )
                return True

            if state == "queued" and self.interrupt_queued_delivery:
                log.debug(
                    "Queued delivery detected for '%s' via %s adapter; interrupting",
                    session_name,
                    self.runtime.value,
                )
                if not await _send_escape_key(session_name):
                    return False
                await asyncio.sleep(_SUBMIT_RECHECK_DELAY_SECONDS)
                if not await _send_submit_key(session_name):
                    return False
                await asyncio.sleep(_SUBMIT_RECHECK_DELAY_SECONDS)
                state = await self.delivery_submission_state(session_name)
                break

        if state == "queued" and not self.interrupt_queued_delivery:
            log.info(
                "Terminal delivery queued in '%s' via %s adapter (runtime will run it next)",
                session_name,
                self.runtime.value,
            )
            return True

        if state != "submitted":
            log.warning(
                "Terminal delivery remained unsent in '%s' via %s adapter (state=%s)",
                session_name,
                self.runtime.value,
                state,
            )
            return False

        log.info(
            "Terminal delivery sent to '%s' via %s adapter",
            session_name,
            self.runtime.value,
        )
        return True

    async def delivery_submission_state(self, session_name: str) -> str:
        """Best-effort state after a submit attempt."""
        pane_content = await capture_pane(session_name, lines=30)
        if not pane_content:
            return "submitted"

        lowered = sanitize_pane_content(pane_content).lower()
        if any(marker in lowered for marker in self.queue_markers):
            return "queued"
        if self.prompt_has_pending_input(pane_content):
            # Input left in the box while the runtime is working is queued by
            # runtimes that support it; otherwise it is simply unsent.
            if self.detect_busy(pane_content):
                return "queued"
            return "prompt_buffered"
        return "submitted"

    def matches_runtime(self, pane_content: str) -> bool:
        """Whether the pane appears to belong to this runtime."""
        lowered = _runtime_analysis_text(pane_content)
        return any(marker in lowered for marker in self.runtime_markers)

    def _matches_prompt_line(self, line: str) -> bool:
        lowered = line.lower()
        return any(lowered.startswith(prefix) for prefix in self.prompt_prefixes) or any(
            lowered.endswith(suffix) for suffix in self.prompt_suffixes
        )

    def _is_status_chrome_line(self, line: str) -> bool:
        lowered = line.lower()
        return any(fragment in lowered for fragment in self.status_fragments)

    def _detect_idle_placeholder_line(self, pane_content: str) -> str | None:
        lowered = sanitize_pane_content(pane_content).lower()
        for fragment in self.placeholder_fragments:
            if fragment in lowered:
                return fragment
        return None


class ClaudeCodeAdapter(TerminalAdapter):
    runtime = TerminalRuntime.CLAUDE
    prompt_prefixes = ("\u276f",)
    prompt_suffixes = ("$", "%")
    runtime_markers = ("claude code", "claude max", "/effort", "shift+tab to cycle")
    status_fragments = (
        "for shortcuts",
        # Status-bar form only — a bare "/effort" would also match the command
        # typed at the prompt and hide the human's pending input.
        "· /effort",
        "accept edits on",
        "auto mode on",
        "shift+tab to cycle",
    )
    busy_markers = ("esc to interrupt",)
    prompt_markers = (
        "do you want to proceed?",
        "do you want to make this edit",
        "do you trust the files in this folder",
        "quick safety check",
        "yes, i trust this folder",
        "yes, proceed",
        "yes, allow",
        "yes, and don't ask again",
        "would you like to proceed",
    )
    submit_attempts = _MAX_SUBMIT_ATTEMPTS
    paste_settle_seconds = 0.2


class GeminiAdapter(TerminalAdapter):
    runtime = TerminalRuntime.GEMINI
    prompt_prefixes = (">",)
    runtime_markers = (
        "gemini cli",
        "gemini code assist",
        "[insert]",
        "press 'esc' for normal mode",
    )
    placeholder_fragments = ("press 'esc' for normal mode",)
    status_fragments = (
        "[insert]",
        "shift+tab to accept edits",
        "? for shortcuts",
        "gemini 3",
    )
    busy_markers = ("esc to cancel",)
    prompt_markers = ("allow execution", "yes, allow once", "yes, allow always")
    submit_attempts = _MAX_SUBMIT_ATTEMPTS
    paste_settle_seconds = 0.2


class CodexAdapter(TerminalAdapter):
    runtime = TerminalRuntime.CODEX
    prompt_prefixes = ("\u203a",)
    runtime_markers = ("openai codex", "gpt-5.", "context left")
    placeholder_fragments = ("implement {feature}", "explain this codebase")
    status_fragments = (
        "gpt-5.",
        "context left",
        "for shortcuts",
        "messages to be submitted after next tool call",
    )
    queue_markers = (
        "tab to queue message",
        "messages to be submitted after next tool call",
        "press esc to interrupt and send immediately",
    )
    busy_markers = ("esc to interrupt",)
    prompt_markers = ("approve this command", "allow command", "yes, and don't ask again")
    submit_attempts = _MAX_SUBMIT_ATTEMPTS
    interrupt_queued_delivery = True
    paste_settle_seconds = 0.2


class CursorAdapter(TerminalAdapter):
    runtime = TerminalRuntime.CURSOR
    prompt_prefixes = ("\u276f", ">", "$", "%")
    runtime_markers = ("cursor", "cursor agent", "cursor composer")
    status_fragments = ("for shortcuts", "accept edits on")
    submit_attempts = _MAX_SUBMIT_ATTEMPTS
    paste_settle_seconds = 0.2


class OpenCodeAdapter(TerminalAdapter):
    runtime = TerminalRuntime.OPENCODE
    runtime_markers = ("opencode", "ask anything...", "ctrl+t variants", "tab agents")
    placeholder_fragments = ("ask anything...",)
    status_fragments = ("ctrl+t variants", "tab agents", "ctrl+p commands")
    submit_attempts = _MAX_SUBMIT_ATTEMPTS
    paste_settle_seconds = 0.2


class AiderAdapter(TerminalAdapter):
    runtime = TerminalRuntime.AIDER
    prompt_prefixes = ("aider>", ">")
    runtime_markers = ("aider", "aider v", "model:", "/help")
    status_fragments = ("tokens:", "cost:")
    prompt_markers = ("(y)es/(n)o", "[y/n]", "(y/n)")
    submit_attempts = _MAX_SUBMIT_ATTEMPTS
    paste_settle_seconds = 0.2


class ShellAdapter(TerminalAdapter):
    """Plain shells: classic ``$``/``%`` prompts and modern ``❯``/``›`` themes."""

    runtime = TerminalRuntime.SHELL
    prompt_prefixes = ("\u276f", "\u203a", "$ ", "% ", "> ", "# ")
    prompt_suffixes = ("$", "%", ">", "#", "\u276f", "\u203a")
    submit_attempts = _MAX_SUBMIT_ATTEMPTS
    paste_settle_seconds = 0.2


_ADAPTERS: dict[TerminalRuntime, TerminalAdapter] = {
    TerminalRuntime.CLAUDE: ClaudeCodeAdapter(),
    TerminalRuntime.GEMINI: GeminiAdapter(),
    TerminalRuntime.CODEX: CodexAdapter(),
    TerminalRuntime.CURSOR: CursorAdapter(),
    TerminalRuntime.OPENCODE: OpenCodeAdapter(),
    TerminalRuntime.AIDER: AiderAdapter(),
    TerminalRuntime.SHELL: ShellAdapter(),
    TerminalRuntime.UNKNOWN: ShellAdapter(),
}


def get_terminal_adapter(runtime: TerminalRuntime | str) -> TerminalAdapter:
    """Return the adapter for a known runtime."""
    runtime_enum = runtime if isinstance(runtime, TerminalRuntime) else normalize_runtime(runtime)
    return _ADAPTERS.get(runtime_enum, _ADAPTERS[TerminalRuntime.SHELL])


def detect_runtime_from_pane(pane_content: str) -> TerminalRuntime:
    """Best-effort runtime detection from visible pane content."""
    if not pane_content.strip():
        return TerminalRuntime.UNKNOWN

    for runtime in (
        TerminalRuntime.GEMINI,
        TerminalRuntime.OPENCODE,
        TerminalRuntime.AIDER,
        TerminalRuntime.CURSOR,
        TerminalRuntime.CODEX,
        TerminalRuntime.CLAUDE,
    ):
        adapter = get_terminal_adapter(runtime)
        if adapter.matches_runtime(pane_content):
            return runtime

    prompt_runtime = _runtime_from_prompt_line(pane_content)
    if prompt_runtime != TerminalRuntime.UNKNOWN:
        return prompt_runtime

    shell_adapter = get_terminal_adapter(TerminalRuntime.SHELL)
    if shell_adapter.detect_idle(pane_content):
        return TerminalRuntime.SHELL

    return TerminalRuntime.UNKNOWN


async def resolve_terminal_runtime(
    session_name: str,
    *,
    runtime_hint: str | None = None,
    pane_content: str | None = None,
) -> TerminalRuntime:
    """Resolve the runtime for a tmux session using hint, env, then prompt."""
    hinted = normalize_runtime(runtime_hint)
    if hinted != TerminalRuntime.UNKNOWN:
        return hinted

    env_runtime = normalize_runtime(await query_environment_var(session_name, RUNTIME_ENV_KEY))
    if env_runtime != TerminalRuntime.UNKNOWN:
        return env_runtime

    if pane_content is None:
        pane_content = await capture_pane(session_name, lines=80)

    return detect_runtime_from_pane(pane_content)


async def get_terminal_adapter_for_session(
    session_name: str,
    *,
    runtime_hint: str | None = None,
    pane_content: str | None = None,
) -> TerminalAdapter:
    """Resolve and return the adapter for a running session."""
    runtime = await resolve_terminal_runtime(
        session_name,
        runtime_hint=runtime_hint,
        pane_content=pane_content,
    )
    return get_terminal_adapter(runtime)


def prompt_has_pending_input(pane_content: str, runtime_hint: str | None = None) -> bool:
    """Whether the current runtime prompt holds buffered input."""
    runtime = normalize_runtime(runtime_hint)
    if runtime == TerminalRuntime.UNKNOWN:
        runtime = detect_runtime_from_pane(pane_content)
    return get_terminal_adapter(runtime).prompt_has_pending_input(pane_content)
