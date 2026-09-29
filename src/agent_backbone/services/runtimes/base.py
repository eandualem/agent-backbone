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
import codecs
import hashlib
import json
import logging
import re
import shutil
from dataclasses import asdict, dataclass
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
    sgr_attributes,
)
from agent_backbone.services.terminal import (
    capture_pane,
    paste_message,
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


@dataclass(frozen=True)
class RuntimeDiagnostic:
    """A classified observation, independent of whether the agent is currently blocked.

    Fields contain codes and model identifiers only. Terminal text and provider
    response bodies are deliberately excluded from this contract.
    """

    code: str
    severity: Literal["info", "warning", "error"] = "error"
    reason: str | None = None
    error_type: str | None = None
    model: str | None = None
    http_status: int | None = None
    observed_effort: str | None = None

    @property
    def observation_key(self) -> str:
        """Coalesce identical metadata while retaining later model/error changes."""
        encoded = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()


def _error_foreground(raw: str) -> bool:
    """Whether visible text has a red foreground (ANSI, palette or true color)."""
    red = False
    for part in re.split(r"(\x1b\[[0-9;]*m)", raw):
        if not part.startswith("\x1b["):
            if red and any(ch.isalpha() for ch in part):
                return True
            continue
        for attribute in sgr_attributes(part[2:-1]):
            code = attribute[0]
            if code in (0, 39) or 30 <= code <= 37 or 90 <= code <= 97:
                red = code in (31, 91)
            elif code == 38 and len(attribute) == 5 and attribute[1] == 2:
                r, g, b = attribute[2:]
                red = r > 100 and r > g * 1.3 and r > b * 1.3
            elif code == 38 and len(attribute) == 3 and attribute[1] == 5:
                color = attribute[2]
                cube = color - 16
                red = color in (1, 9) or (
                    16 <= color <= 231 and cube // 36 > max(cube // 6 % 6, cube % 6)
                )
    return False


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


def has_text(path: Path) -> bool:
    """Whether ``path`` is a file with more than whitespace (Unicode, and a byte
    order mark, as the CLIs trim it) in it, read in chunks up to the first that
    has some."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    try:
        if not path.is_file():
            return False
        with path.open("rb") as handle:
            while chunk := handle.read(64 * 1024):
                if decoder.decode(chunk).strip().strip("\ufeff"):
                    return True
    except OSError:
        return False
    return bool(decoder.decode(b"", final=True).strip())


def git_root(directory: Path) -> Path | None:
    """The nearest directory, ``directory`` itself included, with a ``.git``."""
    return next((d for d in (directory, *directory.parents) if (d / ".git").exists()), None)


def main_checkout(root: Path) -> Path | None:
    """The main checkout of a git worktree at ``root`` (its ``.git`` is a file
    naming the worktree's git directory, whose ``commondir`` leads back)."""
    try:
        line = (root / ".git").read_text().strip()
        if not line.startswith("gitdir:"):
            return None
        gitdir = root / line.removeprefix("gitdir:").strip()
        common = gitdir / (gitdir / "commondir").read_text().strip()
    except (OSError, ValueError):
        return None
    return common.resolve().parent


def agent_home(env: dict[str, str]) -> Path:
    """The home directory the agent's CLI sees: its own ``HOME``, else ours."""
    return Path(env["HOME"]).expanduser() if env.get("HOME") else Path.home()


def split_model_effort(spec: str | None) -> tuple[str | None, str | None]:
    """Split a ``model[:effort]`` spec into ``(model, effort)``.

    The effort is the text after the last colon. Call ``Runtime.split_model``
    to account for runtimes with literal model tags. Carrying the effort in
    the model spec is what lets every
    surface that already names a model name an effort too — ``--model``,
    ``agent set model=…`` and a swarm roster entry
    (``coordinator@codex/gpt-6-astra:high``) — without a second field.
    """
    if not spec:
        return None, None
    model, separator, effort = spec.rpartition(":")
    if not separator:
        return spec, None
    return (model or None), (effort.strip().lower() or None)


class SubmissionUnconfirmed(RuntimeError):
    """Text may already belong to the runtime; never blindly paste it again."""


@dataclass(frozen=True)
class TranscriptEntry:
    """One user-facing message of the agent, complete, from its runtime's own record."""

    time: str
    """``HH:MM:SS`` (UTC) when the record carries a timestamp, else empty."""
    role: str
    """``assistant`` — the agent speaking to the person (progress, commentary, replies)."""
    text: str
    """The whole message text, never shortened."""
    start: int = 0
    """Byte offset where the record begins in the transcript file (the
    record's own position when the runtime keeps a database instead)."""
    end: int = 0
    """Offset just after the record (the cursor to continue from)."""


def transcript_clock(timestamp: object) -> str:
    """``HH:MM:SS`` out of an ISO 8601 timestamp, or empty."""
    if not isinstance(timestamp, str) or "T" not in timestamp:
        return ""
    clock = timestamp.split("T", 1)[1]
    return clock[:8] if len(clock) >= 8 and clock[2] == ":" else ""


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
    brief_refresh: Literal["hook_context", "none"] = "none"
    """How a resumed session gets its current brief (it keeps the one it
    started with): handed to its ``SessionStart`` hook as context, which a
    peer's message cannot imitate, or not at all (a gap: start the agent
    fresh to apply a changed brief). Never as a chat message (#294)."""
    declared_capabilities: frozenset[str] = frozenset()
    """Contract rows (``capabilities.py``) this CLI provides by itself, where
    no other adapter attribute shows it (loading ``AGENTS.md``, declining a
    forged brief): the registry test holds the contract to these."""
    models: tuple[str, ...] = ()
    """Model ids known to work with ``--model`` (aliases or ids seen live).
    Examples for `backbone runtimes`, not an exhaustive list — the CLI's own
    model picker is the authority."""
    efforts: tuple[str, ...] = ()
    """Reasoning-effort levels this runtime can be launched with, as its own
    CLI spells them (verified live against that CLI). Empty means the CLI has
    no effort switch, and an effort asked of it is refused rather than
    silently dropped."""
    model_tags: bool = False
    """Model IDs use literal colon tags, so ``:suffix`` is not an effort setting."""
    skill_dirs: tuple[str, ...] = ()
    """Project-relative directories this CLI reads skills from, measured live
    (``docs/skills.md``). Empty: the backbone materialises no skills for it."""
    unattended_args: tuple[str, ...] | None = None
    """The CLI's own switch that stops it asking a person before acting — the
    launch arguments for an ``unattended`` agent, as the CLI spells them
    (verified live). ``None`` means the backbone knows no such switch for
    this runtime, and an unattended start is refused rather than launched
    attended: an agent that looks configured and is not would park on its
    first dialog."""
    sandboxed: bool = False
    """Whether the CLI confines the commands it runs to the agent's directory
    with an OS sandbox (Codex: Seatbelt on macOS, Landlock on Linux). Behind
    a sandbox, ``unattended`` means free inside a wall; without one it means
    free on the machine — which is why a swarm only makes sandboxed members
    unattended by default."""
    mouse_scroll: bool = False
    """Enable tmux mouse handling for this runtime's session so wheel events
    scroll terminal history. False leaves the user's tmux setting alone."""
    auto_review_args: tuple[str, ...] = ()
    """Opt into the runtime's permission reviewer while retaining its sandbox.
    Empty means no support; unattended mode takes precedence."""

    # --- pane recognition -------------------------------------------------
    prompt_prefixes: tuple[str, ...] = ()
    prompt_suffixes: tuple[str, ...] = ()
    runtime_markers: tuple[str, ...] = ()
    placeholder_fragments: tuple[str, ...] = ()
    status_fragments: tuple[str, ...] = ()
    queue_markers: tuple[str, ...] = ()
    busy_markers: tuple[str, ...] = ()
    """Fragments shown only while the runtime is working (e.g. a spinner line)."""
    provider_error_patterns: tuple[str, ...] = ()
    """Anchored error-banner patterns for provider capacity, quota or rate limits."""
    provider_error_prefixes: tuple[str, ...] = ()
    """Runtime error glyphs distinguish banners from ordinary response text."""
    interrupt_patterns: tuple[str, ...] = ()
    """Anchored patterns for the line a runtime leaves when a person interrupts
    its turn and no hook reports it. Empty: the hooks report interrupts, and
    the terminal is not read for one while a hook state is fresh."""
    notice_fragments: tuple[str, ...] = ()
    """Fragments of the notices a runtime draws at the right edge above its
    input box, which may follow an interrupt line (lowercase)."""
    prompt_markers: tuple[str, ...] = ()
    """Fragments shown when the runtime is asking the human a yes/no question."""
    approve_keys: tuple[str, ...] = ()
    """tmux key names that accept the runtime's permission prompt as shown
    (verified against a live capture of that dialog). Empty: the backbone
    does not know how to answer this runtime and refuses to guess."""
    deny_keys: tuple[str, ...] = ()
    """tmux key names that refuse the permission prompt as shown (same rule:
    verified live, or empty and refused)."""
    choice_markers: tuple[str, ...] = ()
    """Fragments of a dialog whose affirmative key does something other than
    allow a tool — a model switch, a picker. Such a dialog is reported as a
    question and never approved: ``Enter`` there would choose, not permit."""
    plan_approve_keys: tuple[str, ...] = ()
    """tmux key names that accept the plan the runtime is presenting (Claude
    Code, Codex: "1"). Empty: the runtime has no plan mode the backbone can
    drive, and every plan action is refused as unsupported — nothing is typed."""
    plan_reject_keys: tuple[str, ...] = ()
    """tmux key names that decline the plan so feedback can follow as a message
    (the runtime stays in plan mode and revises the plan)."""
    plan_markers: tuple[str, ...] = ()
    """Fragments of the dialog in which the runtime asks to approve its plan,
    all of them present (lowercase). Such a dialog is a plan decision, not a
    question. Empty: the runtime's hooks alone report a plan (OpenCode's plugin)."""

    # --- state hooks ---------------------------------------------------------
    hook_script: str | None = None
    """The shipped hook for this runtime (a file in ``hooks/``), copied into
    ``<data_dir>/hooks/`` at launch. ``None``: the terminal is the only
    source of state."""
    hook_events: hooks.Events = ()
    """``(event, matcher)`` pairs the hook listens to, in the CLI's own names."""
    hook_context: bool = False
    """The hook can add context to the model mid-turn (``hookSpecificOutput.
    additionalContext``; OpenCode's plugin adds a user message to the running
    turn): how a steer or a high-priority subscription batch reaches a working
    agent without a paste. False: a batch waits for the prompt, a steer is refused."""
    hook_timeout: int = 10
    """Per-hook timeout in the unit the CLI uses (seconds for Claude Code and
    Codex, milliseconds for Gemini CLI)."""

    # --- paste behaviour ---------------------------------------------------
    # Observations after the one Enter, 0.1 s apart. Claude Code 2.1.280 was
    # measured taking about 0.5 s to clear a submitted prompt; two checks
    # reported submitted prompts as unconfirmed.
    submission_checks: int = 10
    paste_settle_seconds: float = 0.2

    def __repr__(self) -> str:
        return f"<Runtime {self.id}>"

    # --- launch --------------------------------------------------------------

    def available(self) -> bool:
        """Whether the binary is installed (a shell always is)."""
        return self.binary is None or resolve_command(self.binary) is not None

    def user_instructions(self, env: dict[str, str], project: Path | None = None) -> list[Path]:
        """The user-level instruction files this CLI adds to its sessions, as it
        would pick them now, with content only (#274). ``env`` holds the agent's
        own overrides of the process environment; ``project``, when given, is the
        directory the session starts in."""
        return []

    def native_memory(self, env: dict[str, str], project: Path | None = None) -> str | None:
        """The CLI's own memory across sessions, when the settings Backbone can
        read leave it on for an agent (#292): what it is and how to turn it
        off. ``None`` when it is off or the CLI keeps none. ``env`` and
        ``project`` as for ``user_instructions``."""
        return None

    def pre_trust(self, directory: Path | str) -> None:
        """Answer the runtime's folder-trust dialog ahead of launch, if it has one."""

    def prepare_unattended(self) -> None:
        """Record any consent needed for an explicitly unattended launch."""

    def launch_env(self, model: str | None, effort: str | None = None) -> dict[str, str]:
        """Extra environment the session needs (runtimes that take the model or
        the effort from a variable)."""
        return {}

    def split_model(self, spec: str | None) -> tuple[str | None, str | None]:
        """Interpret a model using this runtime's tag or effort syntax."""
        if self.model_tags:
            return spec or None, None
        return split_model_effort(spec)

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
        self,
        data_dir: Path | str | None,
        state_dir: Path | str | None,
        *,
        env: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Hook overrides, composed with the session's existing ``env`` when needed."""
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

    def effort_args(self, effort: str | None) -> list[str]:
        """Arguments that set the reasoning effort for this launch.

        The base runtime knows no effort switch, so asking one of a runtime
        that has none is an error: silently dropping it would hand back an
        agent that looks configured and is not.
        """
        if not effort:
            return []
        raise RuntimeError(
            f"Runtime '{self.id}' has no effort setting — drop the ':{effort}' from "
            f"the model, or pick a runtime that has one (`backbone runtimes`)"
        )

    def check_effort(self, effort: str | None) -> None:
        """Raise unless ``effort`` is one this runtime accepts."""
        if not effort:
            return
        if not self.efforts:
            raise RuntimeError(
                f"Runtime '{self.id}' has no effort setting — drop the ':{effort}' from "
                f"the model, or pick a runtime that has one (`backbone runtimes`)"
            )
        if effort not in self.efforts:
            raise RuntimeError(
                f"Runtime '{self.id}' has no effort '{effort}' — expected one of "
                f"{', '.join(self.efforts)}"
            )

    def check_unattended(self, unattended: bool) -> None:
        """Raise unless this runtime can be launched without approval prompts."""
        if unattended and self.unattended_args is None:
            raise RuntimeError(
                f"Runtime '{self.id}' has no unattended switch the backbone knows — "
                f"set unattended=false or pick a runtime that has one (`backbone runtimes`)"
            )

    def writable_dir_args(self, dirs: tuple[str, ...]) -> list[str]:
        """Arguments that open ``dirs`` for writing inside the runtime's sandbox.

        Only a sandboxed runtime has anything to open; for the others every
        directory is already writable, so there is nothing to say.
        """
        return []

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
        unattended: bool = False,
        writable_dirs: tuple[str, ...] = (),
        auto_review: bool = False,
    ) -> list[str] | None:
        """The launch command, or None for a plain shell.

        ``brief_file`` is only handed over when ``brief_mode`` injects at
        launch; on ``resume`` initial-prompt runtimes get none here. A
        resumed session keeps its original brief, Claude Code's stored
        system prompt included; a runtime with ``brief_refresh`` is handed
        the current one through its hook instead. ``unattended`` adds the runtime's own
        no-approval switch (``unattended_args``) and ``writable_dirs`` the
        directories a sandboxed runtime may write outside the agent's own,
        both on a fresh start and on ``resume`` alike. Raises RuntimeError
        when the binary is missing, or when ``unattended`` is asked of a
        runtime without such a switch.
        """
        self.check_unattended(unattended)  # a shell has no switch either
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
        model_id, effort = self.split_model(model)
        if effort and not model_id:
            # `:high` would otherwise pass validation and launch the CLI's
            # default model — a spec that names an effort must name a model.
            raise RuntimeError(f"model spec '{model}' names an effort but no model")
        self.check_effort(effort)
        return [
            resolved,
            *self.effort_args(effort),
            *(self.unattended_args if unattended else self.auto_review_args if auto_review else ()),
            *self.writable_dir_args(writable_dirs),
            *self.launch_args(
                model=model_id,
                resume=resume,
                brief_file=brief,
                pre_trust=pre_trust,
                data_dir=data_dir,
                state_dir=state_dir,
            ),
        ]

    # --- pane recognition ----------------------------------------------------

    supports_exact_resume: bool = False
    """Whether a saved session ID is honored rather than selecting runtime-latest."""

    fallback_prompts: tuple[str, ...] = ()

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

    def provider_failure(self, pane_content: str) -> str | None:
        """Current provider error, excluding quoted examples and superseded output."""
        # Join visually wrapped banner continuations before scanning backwards.
        # A new response glyph or prompt always starts a new logical line.
        lines: list[str] = []
        for raw in pane_content.splitlines()[-25:]:
            clean = sanitize_pane_content(raw)
            previous = sanitize_pane_content(lines[-1]).strip() if lines else ""
            banner = previous.lstrip("│┃■●⏺⎿✕✖! ").strip()
            if (
                clean.startswith("  ")
                and clean.strip()
                and not clean.strip().startswith(
                    ("●", "■", "⎿", "✕", *self.provider_error_prefixes, *self.prompt_prefixes)
                )
                and not self._is_status_chrome_line(clean.strip())
                and any(re.match(p, banner, re.I) for p in self.provider_error_patterns)
                and (
                    previous.startswith(self.provider_error_prefixes)
                    or _error_foreground(lines[-1])
                )
            ):
                lines[-1] += " " + clean.strip()
            else:
                lines.append(raw)
        detail: list[str] = []
        for raw in reversed(lines):
            line = sanitize_pane_content(raw).strip()
            if not line or is_box_line(line) or self._is_status_chrome_line(line):
                continue
            if line in self.prompt_prefixes or line.lower() in self.placeholder_fragments:
                continue
            text = line.lstrip("│┃■●⏺⎿✕✖! ").strip()
            if any(
                re.match(pattern, text, re.IGNORECASE) for pattern in self.provider_error_patterns
            ):
                # The words alone may be a final answer quoting an error. Require
                # the runtime's banner glyph or red error foreground as well.
                if not line.startswith(self.provider_error_prefixes) and not _error_foreground(raw):
                    return None
                return "\n".join([text, *reversed(detail)])[:500]
            if re.match(
                r"(?:please retry|try again|retrying|resets? (?:at|in)|https://)", text, re.I
            ):
                detail.append(text)
                continue
            # A later response/tool output means the earlier error is history.
            return None
        return None

    def detect_interrupted(self, pane_content: str) -> bool:
        """Whether the prompt is back because a person interrupted the turn.

        The interrupt line must head the latest output above the prompt, with
        nothing working and no dialog on screen: a newer turn below it is
        history. A block's head starts at most two columns in. Below it may
        follow only its own wrap (indented to its text, joined before
        matching) and known notices (``notice_fragments``) ending two columns
        short of the input box's border, as Claude Code draws them (live
        captures, 80 to 202 columns). Any other line is newer output.
        """
        if not self.interrupt_patterns or not self.detect_idle(pane_content):
            return False
        lines = [sanitize_pane_content(raw).rstrip() for raw in pane_content.splitlines()]
        # Blank padding may fill the pane above the input box.
        lines = [line for line in lines if line.strip()][-40:]
        prompt = max(
            (i for i, line in enumerate(lines) if line.lstrip().startswith(self.prompt_prefixes)),
            default=None,
        )
        above = lines[:prompt]
        border = next((len(line) for line in reversed(above) if is_box_line(line.strip())), None)
        below: list[str] = []
        for line in reversed(above):
            text = line.strip()
            if is_box_line(text) or self._is_status_chrome_line(text):
                continue
            indent = len(line) - len(line.lstrip(" "))
            if indent > 2:
                below.insert(0, line)
                continue
            head = re.match(r"\s*\S+\s+", line)
            column = head.end() if head else indent
            notices = False
            for more in below:
                if (
                    border is not None
                    and len(more) == border - 2
                    and any(fragment in more.lower() for fragment in self.notice_fragments)
                ):
                    notices = True
                elif notices or len(more) - len(more.lstrip(" ")) != column:
                    return False
                else:
                    text += " " + more.strip()
            return any(re.match(pattern, text) for pattern in self.interrupt_patterns)
        return False

    def diagnostics(self, pane_content: str) -> tuple[RuntimeDiagnostic, ...]:
        """Recognized terminal observations; they never make a delivery/state decision."""
        if self.provider_failure(pane_content):
            return (RuntimeDiagnostic(code="provider_failure", reason="provider"),)
        return ()

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

    def detect_permission_prompt(self, pane_content: str) -> bool:
        """Whether the dialog on screen is a known permission prompt, not
        some other question (``prompt_markers`` near the end of the pane)."""
        lowered = sanitize_pane_content(pane_content).lower()[-2000:]
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

    @staticmethod
    def _dialog_block(pane_content: str) -> tuple[list[str], list[str]]:
        """``(above, options)`` of the dialog on screen: the lines leading up
        to its numbered options (at most eight) and the options themselves.
        Anything earlier in the pane is not the dialog, nor is anything above
        the frame a dialog is drawn in (Gemini CLI: ``╭─╮``, ``│ … │`` sides)."""
        lines: list[str] = []
        for raw in sanitize_pane_content(pane_content).strip().splitlines()[-24:]:
            line = raw.strip()
            if line.startswith("╭"):
                lines.clear()
                continue
            while line[:1] == "│" or line[-1:] == "│":
                line = line.strip("│").strip()
            if line and not is_box_line(line.strip("╭╮╰╯")):
                lines.append(line)
        first = next((i for i, ln in enumerate(lines) if DIALOG_OPTION_RE.match(ln)), None)
        if first is None:
            return [], []
        return lines[:first][-8:], lines[first:]

    def detect_choice_dialog(self, pane_content: str) -> bool:
        """Whether the active dialog is a choice (see ``choice_markers``).

        Only the dialog's own text counts — its options and the few lines
        introducing them — so stale output further up the pane cannot turn
        a real permission prompt into a choice."""
        if not self.choice_markers or not self.detect_active_dialog(pane_content):
            return False
        above, options = self._dialog_block(pane_content)
        text = " ".join([*above[-4:], *options]).lower()
        return any(marker in text for marker in self.choice_markers)

    def detect_plan_dialog(self, pane_content: str) -> bool:
        """Whether the active dialog asks to approve a plan (see ``plan_markers``),
        read from the dialog's own text as ``detect_choice_dialog`` reads it."""
        if not self.plan_markers or not self.detect_active_dialog(pane_content):
            return False
        above, options = self._dialog_block(pane_content)
        text = " ".join([*above[-4:], *options]).lower()
        return all(marker in text for marker in self.plan_markers)

    def dialog_summary(self, pane_content: str, *, limit: int = 300) -> str:
        """What the dialog on screen asks, for a person who cannot see it.

        The lines above the dialog's first numbered option — the command,
        the runtime's stated reason — with box chrome dropped, keeping the
        end when longer than ``limit``. Runtime output: relay it as a
        preview, never read it as instruction.
        """
        above, _ = self._dialog_block(pane_content)
        text = " ".join(above)
        if len(text) <= limit:
            return text
        if limit <= 1:
            return text[:limit]
        return "…" + text[-(limit - 1) :]

    def detect_idle(self, pane_content: str) -> bool:
        """Whether the pane currently shows an interactive prompt surface."""
        if self.detect_busy(pane_content) or self.detect_waiting_for_human(pane_content):
            return False
        return self.detect_prompt(pane_content) is not None

    def prompt_has_pending_input(
        self, pane_content: str, *, include_envelope: bool = False
    ) -> bool:
        """Buffered input; delivery verification also counts backbone envelopes."""
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
        return include_envelope or not remainder.startswith(ENVELOPE_PREFIX)

    def _matches_prompt_line(self, line: str) -> bool:
        lowered = line.lower()
        return any(lowered.startswith(prefix) for prefix in self.prompt_prefixes) or any(
            lowered.endswith(suffix) for suffix in self.prompt_suffixes
        )

    def _is_status_chrome_line(self, line: str) -> bool:
        lowered = line.lower()
        return any(fragment in lowered for fragment in self.status_fragments)

    usage_supported = False

    def usage_paths(self, session_id: str, env: dict[str, str]) -> list[Path]:
        """Locate only the conversation explicitly associated with this agent."""
        return []

    transcript_supported = False
    """The runtime keeps its own conversation record that ``usage_paths``
    locates and ``transcript_entries`` (a JSONL file) or ``transcript_page``
    (a database, or a log that repeats a message when it changes) can read
    (``backbone agent output``)."""

    def transcript_entries(self, records: list[dict]) -> list[TranscriptEntry]:
        """The agent's user-facing messages out of parsed JSONL records, oldest
        first and complete. Each record carries ``_start``/``_end`` byte
        offsets. Tool calls, tool results, reasoning and bookkeeping records
        are not messages and are left out."""
        return []

    def transcript_page(
        self,
        path: Path,
        session_id: str,
        *,
        limit: int,
        since: int | None = None,
        before: int | None = None,
        end: int | None = None,
    ) -> tuple[list[TranscriptEntry], bool, bool] | None:
        """A page of messages out of a record that cannot be read a chunk at
        a time, as ``(messages, more_before, more_after)``, navigated by the
        record's own positions the way ``read_messages`` navigates byte
        offsets. ``None``: the record is JSONL, read with
        ``transcript_entries``."""
        return None

    def usage_children(
        self, path: Path, session_id: str, env: dict[str, str]
    ) -> list[tuple[str, Path]]:
        """Runtime-proven child conversations, never inferred from shared directories."""
        return []

    def read_usage(self, path: Path, offset: int, state: dict):
        """Read a bounded chunk from this runtime's usage source."""
        from agent_backbone.services.runtimes._usage import read_jsonl

        return read_jsonl(path, offset, state, self.parse_usage)

    def parse_usage(self, record: dict, state: dict):
        """Project one source record into numeric usage, updating an opaque cursor."""
        return None

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

    async def deny_prompt(self, session_name: str) -> bool:
        """Send the refusing answer to the permission prompt on screen."""
        if not self.deny_keys:
            return False
        for key in self.deny_keys:
            if not await send_keys(session_name, key):
                return False
        return True

    def plan_approval_needs(self, env: dict[str, str]) -> str | None:
        """What an agent's environment (its ``env``, then the backbone's) lacks
        before its plans ask for approval, when the runtime's plan step is
        optional; None when nothing is missing."""
        return None

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
        """Decline the plan so feedback can follow as a message; see ``approve_plan``."""
        return await self._send_all(session_name, self.plan_reject_keys)

    async def deliver_message(self, session_name: str, message: str) -> bool:
        """Paste a message and submit it according to runtime-specific rules."""
        if not await paste_message(session_name, message):
            return False

        if self.paste_settle_seconds > 0:
            await asyncio.sleep(self.paste_settle_seconds)

        try:
            state = await self._submit(session_name)
        except Exception as exc:
            raise SubmissionUnconfirmed("Could not confirm pasted text") from exc

        if state in {"submitted", "queued"}:
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
        raise SubmissionUnconfirmed(f"Pasted text has state {state}")

    async def _submit(self, session_name: str) -> str:
        """Submit once, then observe; repeating Enter can interrupt an active turn."""
        state = "submitted"
        if not await press_submit(session_name):
            return "failed"
        for _attempt in range(self.submission_checks):
            await asyncio.sleep(_SUBMIT_RECHECK_DELAY_SECONDS)
            state = await self.delivery_submission_state(session_name)
            if state in {"submitted", "queued"}:
                # Queued means accepted by the runtime. Another Enter or Escape
                # can steer/interfere with the active turn or submit twice.
                return state
        return state

    async def delivery_submission_state(self, session_name: str) -> str:
        """Best-effort state after a submit attempt."""
        pane_content = await capture_pane(session_name, lines=30)
        if not pane_content:
            return "submitted"

        lowered = sanitize_pane_content(pane_content).lower()
        if any(marker in lowered for marker in self.queue_markers):
            return "queued"
        if self.prompt_has_pending_input(pane_content, include_envelope=True):
            # Input left in the box while the runtime is working is queued by
            # runtimes that support it; otherwise it is simply unsent.
            if self.detect_busy(pane_content):
                return "queued"
            return "prompt_buffered"
        return "submitted"
