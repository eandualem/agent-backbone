"""Gemini CLI."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from agent_backbone.hooks.install import save_settings
from agent_backbone.services.runtimes._pane import sanitize_pane_content
from agent_backbone.services.runtimes.base import Runtime, agent_home, has_text, read_brief

log = logging.getLogger(__name__)

_JSON_COMMENT = re.compile(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/', re.S)
"""A JSON string (kept) or a comment (dropped), as Gemini CLI strips them."""

_OPTION = re.compile(r"(?:●\s*)?(\d+)\.\s+(.+)")
"""A numbered option inside a dialog's frame, the cursor (●) optional."""


def _box_title(box: list[str]) -> str:
    """The first text line inside a ``╭ … ╰`` frame, lowercased."""
    for row in box[1:]:
        if text := row.strip().strip("│").strip():
            return text.lower()
    return ""


def _without_update_notice(pane_content: str) -> list[str]:
    """The pane's lines without the "Gemini CLI update available!" box, which
    Gemini can draw below a dialog (live, 0.46)."""
    kept: list[str] = []
    box: list[str] = []
    for line in sanitize_pane_content(pane_content).splitlines():
        if not box and not line.lstrip().startswith("╭"):
            kept.append(line)
            continue
        box.append(line)
        if line.lstrip().startswith("╰"):
            if not _box_title(box).startswith("gemini cli update available!"):
                kept.extend(box)
            box = []
    return [*kept, *box]


def _permission_dialog(pane_content: str) -> bool:
    """Whether the last thing on screen is a tool-permission dialog.

    Recognised by its options, not by any text a reply could also contain:
    option 1 "Allow once" and a last option "No, suggest changes (esc)"
    (live, 0.46: shell, edit, fetch). A question the model asks ends with
    "Enter a custom value" and a hint line instead, even when its title is
    off screen. Only the frame's own rows count: a command or diff sits in
    a nested box, and its rows keep their "│".
    """
    lines = _without_update_notice(pane_content)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or not lines[-1].lstrip().startswith("╰"):
        return False
    rows: list[str] = []
    for line in reversed(lines[:-1]):
        line = line.strip()
        if not line.startswith("│"):
            break
        rows.append(line[1:].removesuffix("│").strip())
    rows.reverse()
    if any(row.lower().startswith(("answer questions", "enter to select")) for row in rows):
        return False
    options = [match.groups() for row in rows if (match := _OPTION.fullmatch(row))]
    return (
        bool(options)
        and options[0] == ("1", "Allow once")
        and options[-1][1] == "No, suggest changes (esc)"
    )


def _context_file_names(settings: Path) -> list[str] | None:
    """``context.fileName`` in one Gemini settings file, None where it sets none."""
    try:
        text = _JSON_COMMENT.sub(lambda m: m[1] or "", settings.read_text())
        configured = json.loads(text)["context"]["fileName"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if isinstance(configured, str):
        return [configured]
    if isinstance(configured, list):
        return [name for name in configured if isinstance(name, str)]
    return None


class Gemini(Runtime):
    supports_exact_resume = True
    id = "gemini"
    skill_dirs = (".agents/skills",)
    display_name = "Gemini CLI"
    aliases = ("gemini-cli",)
    binary = "gemini"
    brief_mode = "initial_prompt"
    declared_capabilities = frozenset({"observed-model", "trust"})

    hook_events = (
        ("SessionStart", None),
        ("SessionEnd", None),
        ("BeforeAgent", None),
        ("AfterAgent", None),
        ("BeforeTool", None),
        ("AfterTool", None),
        ("Notification", None),
    )
    hook_timeout = 10_000  # milliseconds

    prompt_prefixes = (">",)
    runtime_markers = (
        "gemini cli",
        "gemini code assist",
        "[insert]",
        "press 'esc' for normal mode",
        "type your message or @path/to/file",
    )
    # The empty input's placeholder, in grey rather than dim (0.46, live).
    placeholder_fragments = ("press 'esc' for normal mode", "type your message or @path/to/file")
    status_fragments = (
        "[insert]",
        "shift+tab to accept edits",
        "? for shortcuts",
        "gemini 3",
    )
    busy_markers = ("esc to cancel",)
    prompt_markers = (
        "allow execution",
        "yes, allow once",
        "yes, allow always",
        "do you trust the files in this folder",
        "how would you like to authenticate",
        "failed to sign in",
        "waiting for auth",
    )
    # "1" picks "Allow once" wherever the cursor is (live, 0.46); Enter would
    # pick the highlighted option, which a person may have moved.
    approve_keys = ("1",)
    # "3. No, suggest changes (esc)": the request is cancelled and the tool
    # never runs (live, 0.46), as Escape does in Claude Code and Codex.
    deny_keys = ("Escape",)
    # "--approval-mode yolo  auto-approve all tools" (gemini-cli --help). No
    # OS sandbox behind it: trust on the machine.
    unattended_args = ("--approval-mode", "yolo")

    # A file edit or a web fetch asks "Apply this change?" or "Do you want to
    # proceed?", which replies ask too: those dialogs are recognised by their
    # options instead (_permission_dialog).
    def detect_waiting_for_human(self, pane_content: str) -> bool:
        return _permission_dialog(pane_content) or super().detect_waiting_for_human(pane_content)

    def detect_active_dialog(self, pane_content: str) -> bool:
        return _permission_dialog(pane_content) or super().detect_active_dialog(pane_content)

    def detect_choice_dialog(self, pane_content: str) -> bool:
        # "1" allows a tool only in a recognised permission dialog. Any other
        # active dialog is a choice, where "1" would pick an answer: the
        # sign-in picker, folder trust, or a question the model asks.
        return self.detect_active_dialog(pane_content) and not _permission_dialog(pane_content)

    @staticmethod
    def _dialog_block(pane_content: str) -> tuple[list[str], list[str]]:
        # The update notice below a dialog must not end the dialog's frame.
        return Runtime._dialog_block("\n".join(_without_update_notice(pane_content)))

    def hook_settings_path(self, project_dir: Path | None) -> Path:
        if project_dir is not None:
            return Path(project_dir).expanduser() / ".gemini" / "settings.json"
        return Path("~/.gemini/settings.json").expanduser()

    def hook_launch_env(
        self,
        data_dir: Path | str | None,
        state_dir: Path | str | None,
        *,
        env: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """``GEMINI_CLI_SYSTEM_SETTINGS_PATH`` → a backbone-owned settings file.

        Gemini CLI merges a system-settings file over the user's and the
        project's; pointing it at ``<data_dir>/hooks/gemini-settings.json``
        wires the hooks for this session only. Nothing in ``~/.gemini`` or the
        repository is touched. Verified live against Gemini CLI 0.46.
        """
        if data_dir is None or state_dir is None:
            return {}
        try:
            _, settings = self.hook_settings(data_dir, state_dir)
            path = Path(data_dir) / "hooks" / "gemini-settings.json"
            save_settings(path, settings)
        except OSError as exc:
            log.warning("Could not write the launch hook settings: %s", exc)
            return {}
        return {"GEMINI_CLI_SYSTEM_SETTINGS_PATH": str(path)}

    def user_instructions(self, env, project=None):
        home = Path(
            env.get("GEMINI_CLI_HOME") or os.environ.get("GEMINI_CLI_HOME") or agent_home(env)
        ).expanduser()
        gemini = home / ".gemini"
        # Every context file name is read from ~/.gemini: the configured
        # `context.fileName` (the project's settings over the user's), then
        # GEMINI.md, which a configured name adds to rather than replaces (0.46).
        configured = (
            None
            if project is None
            else _context_file_names(Path(project) / ".gemini/settings.json")
        )
        if configured is None:  # an empty list in the project still overrides
            configured = _context_file_names(gemini / "settings.json")
        names = dict.fromkeys([*(name.strip() for name in configured or ()), "GEMINI.md"])
        return [gemini / name for name in names if name and has_text(gemini / name)]

    def native_memory(self, env, project=None):
        # 0.46 loads them into every session whatever its prompt settings; its
        # default prompt also has the model write them.
        return (
            "it loads GEMINI.md files and a private per-project MEMORY.md into every "
            "session, and its default prompt has the model write them (no setting "
            "turns this off)"
        )

    def launch_args(self, *, model, resume, brief_file, pre_trust, data_dir, state_dir):
        args: list[str] = []
        if model:
            args.extend(["--model", model])
        if resume:
            args.extend(["--resume", resume if isinstance(resume, str) else "latest"])
        if pre_trust:
            args.append("--skip-trust")  # Gemini's trust dialog is a flag, not a config file
        if brief_file is not None and (brief := read_brief(brief_file)):
            args.extend(["--prompt-interactive", brief])
        return args


RUNTIME = Gemini()
