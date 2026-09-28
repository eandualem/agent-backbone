"""Gemini CLI."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from agent_backbone.hooks.install import save_settings
from agent_backbone.services.runtimes.base import Runtime, agent_home, has_text, read_brief

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
    # The sign-in picker ("● 1. Sign in with Google", live, 0.46) is a choice:
    # "1" would pick an auth method, not allow a tool.
    choice_markers = ("how would you like to authenticate", "use gemini api key")
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
