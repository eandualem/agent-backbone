"""Gemini CLI."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from agent_backbone.hooks.install import save_settings
from agent_backbone.services.runtimes.base import Runtime, has_text, read_brief

log = logging.getLogger(__name__)

_JSON_COMMENT = re.compile(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/', re.S)
"""A JSON string (kept) or a comment (dropped), as Gemini CLI strips them."""


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
    declared_capabilities = frozenset({"trust"})

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
    )
    placeholder_fragments = ("press 'esc' for normal mode",)
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
    # approve_keys stays empty until the dialog is captured live (README's
    # Gemini note): the backbone answers only what it has seen.
    # "--approval-mode yolo  auto-approve all tools" (gemini-cli --help). No
    # OS sandbox behind it: trust on the machine.
    unattended_args = ("--approval-mode", "yolo")

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
            env.get("GEMINI_CLI_HOME") or os.environ.get("GEMINI_CLI_HOME") or Path.home()
        ).expanduser()
        gemini = home / ".gemini"
        # Every context file name (`context.fileName`, GEMINI.md by default) is
        # read from ~/.gemini (0.46); the project's settings override the user's.
        names = (
            (project is not None and _context_file_names(Path(project) / ".gemini/settings.json"))
            or _context_file_names(gemini / "settings.json")
            or ["GEMINI.md"]
        )
        return [gemini / name for name in names if name and has_text(gemini / name)]

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
