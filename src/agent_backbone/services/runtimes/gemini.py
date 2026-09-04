"""Gemini CLI."""

from __future__ import annotations

import logging
from pathlib import Path

from agent_backbone.hooks.install import save_settings
from agent_backbone.services.runtimes.base import Runtime, read_brief

log = logging.getLogger(__name__)


class Gemini(Runtime):
    id = "gemini"
    display_name = "Gemini CLI"
    aliases = ("gemini-cli",)
    binary = "gemini"
    brief_mode = "initial_prompt"

    hook_script = "gemini_hook.py"
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

    def hook_settings_path(self, project_dir: Path | None) -> Path:
        if project_dir is not None:
            return Path(project_dir).expanduser() / ".gemini" / "settings.json"
        return Path("~/.gemini/settings.json").expanduser()

    def hook_launch_env(
        self, data_dir: Path | str | None, state_dir: Path | str | None
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

    def launch_args(self, *, model, resume, brief_file, pre_trust, data_dir, state_dir):
        args: list[str] = []
        if model:
            args.extend(["--model", model])
        if resume:
            args.extend(["--resume", "latest"])
        if pre_trust:
            args.append("--skip-trust")  # Gemini's trust dialog is a flag, not a config file
        if brief_file is not None and (brief := read_brief(brief_file)):
            args.extend(["--prompt-interactive", brief])
        return args


RUNTIME = Gemini()
