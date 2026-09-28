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

_ALLOW_ONCE_FIRST = re.compile(r"(?:●\s*)?1\.\s+allow once", re.I)
"""A tool-permission dialog's first option (live, 0.46: shell, edit, fetch)."""


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
        # A file edit and a web fetch ask these instead (live, 0.46).
        "apply this change?",
        "do you want to proceed?",
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

    def detect_choice_dialog(self, pane_content: str) -> bool:
        # "1" allows a tool only where option 1 is "Allow once". Every other
        # numbered dialog is a choice, where "1" would pick an answer: the
        # sign-in picker, folder trust, or a question the model asks ("Answer
        # Questions", live, 0.46).
        if not self.detect_active_dialog(pane_content):
            return False
        above, options = self._dialog_block(pane_content)
        if any(line.lower() == "answer questions" for line in above):
            return True
        return not any(_ALLOW_ONCE_FIRST.fullmatch(option) for option in options)

    @staticmethod
    def _dialog_block(pane_content: str) -> tuple[list[str], list[str]]:
        # The "Gemini CLI update available!" box can be drawn below a dialog
        # (live, 0.46); it is not the dialog, so it must not end the dialog's
        # frame.
        kept: list[str] = []
        box: list[str] = []
        for line in sanitize_pane_content(pane_content).splitlines():
            if not box and not line.lstrip().startswith("╭"):
                kept.append(line)
                continue
            box.append(line)
            if line.lstrip().startswith("╰"):
                if not any("update available!" in row.lower() for row in box):
                    kept.extend(box)
                box = []
        return Runtime._dialog_block("\n".join([*kept, *box]))

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
