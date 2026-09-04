"""``Runtime`` — everything the backbone knows about one interactive CLI.

One object per runtime holds it all: how to recognise the CLI in a pane
(prompt, busy marker, permission dialog), how to paste into it, how to
answer its permission prompt, how to launch it (binary, model, resume,
brief injection, trust dialog, state hooks). Adding a runtime is one new
module in this package that subclasses ``Runtime`` and registers itself in
``__init__``.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Literal

from agent_backbone.hooks import install as hooks
from agent_backbone.services.runtimes._pane import (
    DIALOG_CURSOR_RE,
    DIALOG_OPTION_RE,
    ENVELOPE_PREFIX,
    is_box_line,
    prompt_line_is_dim_placeholder,
    prompt_tail_line_pairs,
    runtime_analysis_text,
    sanitize_pane_content,
)
from agent_backbone.services.terminal import (
    capture_pane,
    paste_message,
    press_escape,
    press_submit,
    send_keys,
)

log = logging.getLogger(__name__)

_SUBMIT_RECHECK_DELAY_SECONDS = 0.1

# Fallback directories for binaries not on PATH (common for npm/bun global installs)
_FALLBACK_DIRS = (
    Path.home() / ".bun" / "bin",
    Path.home() / ".local" / "bin",
    Path.home() / ".npm-global" / "bin",
)

BriefMode = Literal["system_prompt", "initial_prompt", "message", "none"]


def resolve_command(name: str) -> str | None:
    """Resolve a command name to an absolute path (PATH first, then fallbacks)."""
    path = shutil.which(name)
    if path:
        return path
    for directory in _FALLBACK_DIRS:
        candidate = directory / name
        if candidate.is_file():
            return str(candidate)
    return None


def read_brief(brief_file: Path | str) -> str | None:
    """The brief's text, or None when it cannot be read (the agent starts without it)."""
    try:
        text = Path(brief_file).read_text().strip()
    except OSError:
        log.warning("Could not read the brief %s (starting without it)", brief_file)
        return None
    return text or None


class Runtime:
    """Behavioural contract for one interactive CLI. Subclasses set the data."""

    id: str = "unknown"
    display_name: str = "Unknown"
    aliases: tuple[str, ...] = ()
    """Other spellings ``get_runtime`` accepts for this runtime."""
    binary: str | None = None
    """Command to launch; ``None`` starts the login shell instead."""
    brief_mode: BriefMode = "message"
    """How the agent brief reaches the runtime at launch: appended to the
    system prompt, passed as the first (initial) prompt, delivered as the
    first message once the agent is at its prompt, or not at all."""
    models: tuple[str, ...] = ()
    """Model ids known to work with ``--model`` (aliases or ids seen live).
    Examples for `backbone runtimes`, not an exhaustive list — the CLI's own
    model picker is the authority."""

    # --- pane recognition -------------------------------------------------
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
    approve_keys: tuple[str, ...] = ()
    """tmux key names that accept the runtime's permission prompt as shown
    (verified against a live capture of that dialog). Empty: the backbone
    does not know how to answer this runtime and refuses to guess."""
    plan_approve_keys: tuple[str, ...] = ()
    """tmux key names that accept the plan the runtime is presenting (Claude
    Code: Shift+Tab). Empty: the runtime has no plan mode the backbone can
    drive, and every plan action is refused as unsupported — nothing is typed."""
    plan_reject_keys: tuple[str, ...] = ()
    """tmux key names that leave plan mode so feedback can follow as a message."""

    # --- state hooks ---------------------------------------------------------
    hook_script: str | None = None
    """The shipped hook for this runtime (a file in ``hooks/``), copied into
    ``<data_dir>/hooks/`` at launch. ``None``: the terminal is the only
    source of state."""
    hook_events: hooks.Events = ()
    """``(event, matcher)`` pairs the hook listens to, in the CLI's own names."""
    hook_timeout: int = 10
    """Per-hook timeout in the unit the CLI uses (seconds for Claude Code and
    Codex, milliseconds for Gemini CLI)."""

    # --- paste behaviour ---------------------------------------------------
    auto_submit: bool = False
    submit_attempts: int = 2
    interrupt_queued_delivery: bool = False
    paste_settle_seconds: float = 0.2

    def __repr__(self) -> str:
        return f"<Runtime {self.id}>"

    # --- launch --------------------------------------------------------------

    def available(self) -> bool:
        """Whether the binary is installed (a shell always is)."""
        return self.binary is None or resolve_command(self.binary) is not None

    def pre_trust(self, directory: Path | str) -> None:
        """Answer the runtime's folder-trust dialog ahead of launch, if it has one."""

    def launch_env(self, model: str | None) -> dict[str, str]:
        """Extra environment the session needs (runtimes that take the model from a variable)."""
        return {}

    @property
    def reports_state(self) -> str:
        """How the backbone learns this runtime's state (for ``backbone runtimes``)."""
        return "hooks + terminal" if self.hook_script else "terminal"

    def hook_launch_args(
        self, data_dir: Path | str | None, state_dir: Path | str | None
    ) -> list[str]:
        """Extra CLI args that wire the runtime's state hooks to the backbone."""
        return []

    def hook_launch_env(
        self, data_dir: Path | str | None, state_dir: Path | str | None
    ) -> dict[str, str]:
        """Extra environment that wires the runtime's state hooks to the backbone."""
        return {}

    def hook_settings(
        self, data_dir: Path | str, state_dir: Path | str, *, python: str | None = None
    ) -> tuple[str, dict]:
        """``(command, settings)``: the hook command for this runtime and the
        ``{"hooks": …}`` document that wires it, after the hook files are in
        ``<data_dir>/hooks/``. Raises ``RuntimeError`` for a runtime without a hook."""
        if not self.hook_script:
            raise RuntimeError(f"runtime '{self.id}' has no state hook")
        hooks_dir = hooks.install_hook_files(Path(data_dir))
        command = hooks.hook_command(
            hooks_dir / self.hook_script, Path(state_dir), python=python or hooks.default_python()
        )
        return command, hooks.merge_hooks({}, self.hook_events, command, self.hook_timeout)

    def hook_settings_path(self, project_dir: Path | None) -> Path | None:
        """Where ``backbone hooks install`` writes for sessions started outside the
        backbone: the user's global file, or a project's. ``None``: no such place."""
        return None

    def install_hooks(
        self,
        data_dir: Path | str,
        state_dir: Path | str,
        *,
        project_dir: Path | None = None,
        python: str | None = None,
    ) -> tuple[Path, str] | None:
        """Add the hooks to the runtime's own settings (``backbone hooks install``).

        Returns ``(settings_path, command)``, or ``None`` when this runtime has
        no settings file the backbone knows how to edit.
        """
        path = self.hook_settings_path(project_dir)
        if path is None or not self.hook_script:
            return None
        command, _ = self.hook_settings(data_dir, state_dir, python=python)
        settings = hooks.load_settings(path)
        hooks.save_settings(
            path, hooks.merge_hooks(settings, self.hook_events, command, self.hook_timeout)
        )
        return path, command

    def uninstall_hooks(self, *, project_dir: Path | None = None) -> Path | None:
        path = self.hook_settings_path(project_dir)
        if path is None:
            return None
        hooks.save_settings(path, hooks.remove_hooks(hooks.load_settings(path)))
        return path

    def launch_args(
        self,
        *,
        model: str | None,
        resume: bool | str,
        brief_file: Path | None,
        pre_trust: bool,
        data_dir: Path | str | None,
        state_dir: Path | str | None,
    ) -> list[str]:
        """Arguments after the binary. The default: ``--model``, ``--resume``.

        ``resume`` is ``True`` for the runtime's own notion of "the last
        conversation", or a session id the backbone saw through the hook;
        a runtime that cannot address a session by id treats it as ``True``.
        """
        args: list[str] = []
        if model:
            args.extend(["--model", model])
        if resume:
            args.append("--resume")
            if isinstance(resume, str):
                args.append(resume)  # claude --resume <session id>
        return args

    def build_command(
        self,
        *,
        model: str | None = None,
        resume: bool | str = False,
        brief_file: Path | str | None = None,
        pre_trust: bool = False,
        data_dir: Path | str | None = None,
        state_dir: Path | str | None = None,
    ) -> list[str] | None:
        """The launch command, or None for a plain shell.

        ``brief_file`` is only handed over when ``brief_mode`` injects at
        launch; a resumed session already has its brief, so initial-prompt
        runtimes are not re-briefed on ``resume`` (a system prompt is
        re-applied every launch). Raises RuntimeError when the binary is
        missing.
        """
        if self.binary is None:
            return None
        resolved = resolve_command(self.binary)
        if resolved is None:
            raise RuntimeError(f"Runtime '{self.id}' binary not found: {self.binary}")
        brief = Path(brief_file) if brief_file is not None else None
        if self.brief_mode == "initial_prompt" and resume:
            brief = None
        if self.brief_mode in ("message", "none"):
            brief = None
        return [
            resolved,
            *self.launch_args(
                model=model,
                resume=resume,
                brief_file=brief,
                pre_trust=pre_trust,
                data_dir=data_dir,
                state_dir=state_dir,
            ),
        ]

    # --- pane recognition ----------------------------------------------------

    def matches(self, pane_content: str) -> bool:
        """Whether the pane appears to belong to this runtime."""
        lowered = runtime_analysis_text(pane_content)
        return any(marker in lowered for marker in self.runtime_markers)

    def detect_prompt(self, pane_content: str) -> str | None:
        """Return the visible prompt line when this runtime recognizes one."""
        for raw_candidate, candidate in reversed(prompt_tail_line_pairs(pane_content)):
            stripped = candidate.strip()
            if not stripped or is_box_line(stripped):
                continue
            if self._is_status_chrome_line(stripped):
                continue
            if DIALOG_CURSOR_RE.match(stripped):
                break  # "❯ 1. …" is a dialog's selected option, not the input prompt
            if self._matches_prompt_line(stripped):
                return raw_candidate.strip()
            break

        lowered = sanitize_pane_content(pane_content).lower()
        for fragment in self.placeholder_fragments:
            if fragment in lowered:
                return fragment
        return None

    def detect_busy(self, pane_content: str) -> bool:
        """Whether the runtime is visibly working (spinner / interrupt hint)."""
        if not self.busy_markers:
            return False
        tail = sanitize_pane_content(pane_content).strip().splitlines()[-12:]
        lowered = "\n".join(line.lower() for line in tail)
        return any(marker in lowered for marker in self.busy_markers)

    def detect_waiting_for_human(self, pane_content: str) -> bool:
        """Whether the runtime is visibly blocked on a question to the human.

        Either a known question (``prompt_markers``) or, whatever the
        question says, a dialog's own chrome (``detect_dialog_chrome``).
        """
        if self.detect_dialog_chrome(pane_content):
            return True
        if not self.prompt_markers:
            return False
        tail = sanitize_pane_content(pane_content).strip().splitlines()[-15:]
        lowered = "\n".join(line.lower() for line in tail)
        return any(marker in lowered for marker in self.prompt_markers)

    def detect_dialog_chrome(self, pane_content: str) -> bool:
        """Whether the tail shows a numbered-option block with a selection cursor.

        Every CLI dialog — permission, folder trust, Claude Code's "resume
        from summary" picker, a model picker — draws the same furniture:
        two or more numbered options, one carrying the cursor. Recognising
        the furniture instead of the wording means a dialog the backbone has
        never seen still reads as ``waiting_for_human`` rather than as an
        idle prompt to paste into.
        """
        lines = [ln.strip() for ln in sanitize_pane_content(pane_content).strip().splitlines()]
        options = [ln for ln in lines[-15:] if DIALOG_OPTION_RE.match(ln)]
        return len(options) >= 2 and any(DIALOG_CURSOR_RE.match(ln) for ln in options)

    def detect_active_dialog(self, pane_content: str) -> bool:
        """Whether a permission dialog is on screen *right now*.

        The stricter gate used before answering one (``approve_prompt``).
        ``detect_waiting_for_human`` is a state reading — a marker anywhere
        in the tail is enough. Answering needs more, because ``Enter`` on an
        idle prompt submits whatever is typed there. So the last marker must
        be the runtime's most recent surface: nothing after it may look like
        an input prompt (empty or with typed text), a placeholder or status
        chrome — only the dialog's own numbered options and hints.
        """
        lines = [ln.strip() for ln in sanitize_pane_content(pane_content).strip().splitlines()]
        lines = lines[-15:]
        chrome = self.detect_dialog_chrome(pane_content)
        last_marker = None
        for i, line in enumerate(lines):
            if any(marker in line.lower() for marker in self.prompt_markers) or (
                chrome and DIALOG_CURSOR_RE.match(line)
            ):
                last_marker = i
        if last_marker is None:
            return False
        for line in lines[last_marker + 1 :]:
            if not line or is_box_line(line):
                continue
            if DIALOG_OPTION_RE.match(line):
                continue  # the dialog's own "❯ 1. Yes" / "› 1. Yes, proceed" cursor line
            lowered = line.lower()
            if self._matches_prompt_line(line) or self._is_status_chrome_line(line):
                return False  # the runtime is back at its input; the dialog is history
            if any(fragment in lowered for fragment in self.placeholder_fragments):
                return False
        return True

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
        if prompt_line_is_dim_placeholder(prompt_line):
            # Dim text after the prompt is a suggestion/placeholder, not typed input.
            return False

        # Prefix guard: if the runtime defines prompt_prefixes and the sanitized
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
        return not remainder.startswith(ENVELOPE_PREFIX)

    def _matches_prompt_line(self, line: str) -> bool:
        lowered = line.lower()
        return any(lowered.startswith(prefix) for prefix in self.prompt_prefixes) or any(
            lowered.endswith(suffix) for suffix in self.prompt_suffixes
        )

    def _is_status_chrome_line(self, line: str) -> bool:
        lowered = line.lower()
        return any(fragment in lowered for fragment in self.status_fragments)

    # --- typing into the session -----------------------------------------------

    async def approve_prompt(self, session_name: str) -> bool:
        """Send the affirmative answer to the permission prompt on screen.

        Callers verify with ``detect_active_dialog`` first: these keys are
        only meaningful while the dialog is visible.
        """
        if not self.approve_keys:
            return False
        for key in self.approve_keys:
            if not await send_keys(session_name, key):
                return False
        return True

    @property
    def supports_plan_control(self) -> bool:
        """Whether the backbone can approve or reject this runtime's plans."""
        return bool(self.plan_approve_keys)

    async def _send_all(self, session_name: str, keys: tuple[str, ...]) -> int:
        """Send keys in order; returns how many went in (tmux refused the next)."""
        sent = 0
        for key in keys:
            if not await send_keys(session_name, key):
                break
            sent += 1
        return sent

    async def approve_plan(self, session_name: str) -> int:
        """Accept the plan on screen. Returns the number of keys sent: all of
        ``plan_approve_keys`` on success, fewer when tmux refused one part-way
        (the caller reports that: the earlier keys may have changed the mode),
        0 when this runtime has no plan mode."""
        return await self._send_all(session_name, self.plan_approve_keys)

    async def reject_plan(self, session_name: str) -> int:
        """Leave plan mode so feedback can follow as a message; see ``approve_plan``."""
        return await self._send_all(session_name, self.plan_reject_keys)

    async def deliver_message(self, session_name: str, message: str) -> bool:
        """Paste a message and submit it according to runtime-specific rules."""
        if not await paste_message(session_name, message):
            return False

        if self.paste_settle_seconds > 0:
            await asyncio.sleep(self.paste_settle_seconds)

        state = "submitted" if self.auto_submit else await self._submit(session_name)

        if state == "submitted" or (state == "queued" and not self.interrupt_queued_delivery):
            log.info(
                "Terminal delivery %s in '%s' via %s",
                "sent" if state == "submitted" else "queued (runtime will run it next)",
                session_name,
                self.id,
            )
            return True
        log.warning(
            "Terminal delivery remained unsent in '%s' via %s (state=%s)",
            session_name,
            self.id,
            state,
        )
        return False

    async def _submit(self, session_name: str) -> str:
        """Press Enter (retrying once where the runtime needs it) and report what happened."""
        state = "submitted"
        for _attempt in range(self.submit_attempts):
            if not await press_submit(session_name):
                return "failed"
            await asyncio.sleep(_SUBMIT_RECHECK_DELAY_SECONDS)
            state = await self.delivery_submission_state(session_name)
            if state == "submitted":
                return state
            if state == "queued" and self.interrupt_queued_delivery:
                # The runtime parked the text for its next turn; Escape exposes
                # it, Enter sends it now.
                if not await press_escape(session_name):
                    return "failed"
                await asyncio.sleep(_SUBMIT_RECHECK_DELAY_SECONDS)
                if not await press_submit(session_name):
                    return "failed"
                await asyncio.sleep(_SUBMIT_RECHECK_DELAY_SECONDS)
                return await self.delivery_submission_state(session_name)
        return state

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
