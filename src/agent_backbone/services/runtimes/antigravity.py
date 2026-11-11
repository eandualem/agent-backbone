"""Antigravity CLI (``agy``), Google's terminal agent for personal Google AI plans.

Gemini CLI no longer serves personal accounts; it keeps its own adapter for
API keys and Code Assist Standard/Enterprise. State comes from the terminal
only: no hooks are wired. Everything below was captured live from 1.5.5.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from agent_backbone.fs import atomic_write_text
from agent_backbone.services.runtimes.base import Runtime, agent_home, read_brief

log = logging.getLogger(__name__)


def pre_trust_workspace(directory: Path | str, settings_file: Path) -> bool:
    """Add a directory to ``trustedWorkspaces`` in Antigravity's settings.

    The same record its trust dialog writes when a person picks "Yes, I
    trust this folder" (1.5.5, live). It matches the exact directory, not
    its subfolders. Best-effort: on any error the dialog appears as before.
    """
    path = str(Path(directory).expanduser().resolve())
    try:
        data = json.loads(settings_file.read_text()) if settings_file.is_file() else {}
        if not isinstance(data, dict):
            return False
        trusted = data.setdefault("trustedWorkspaces", [])
        if not isinstance(trusted, list):
            return False
        if path in trusted:
            return True
        trusted.append(path)
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(settings_file, json.dumps(data, indent=2))
        log.info("Pre-trusted %s for Antigravity CLI", path)
        return True
    except (OSError, ValueError):
        log.warning("Could not pre-trust %s (the trust dialog will appear)", path)
        return False


class Antigravity(Runtime):
    id = "antigravity"
    display_name = "Antigravity CLI"
    aliases = ("agy", "antigravity-cli")
    binary = "agy"
    brief_mode = "initial_prompt"
    declared_capabilities = frozenset({"trust"})
    # `agy models` (1.5.5, Google AI Ultra); the CLI's own list is the authority.
    models = (
        "gemini-4-carbon-medium",
        "gemini-4-argon-medium",
        "gemini-3.8-flash-medium",
        "claude-opus-5-5-high",
    )
    # "--effort  Reasoning effort for the current CLI session" (agy --help).
    efforts = ("low", "medium", "high", "xhigh", "max")

    prompt_prefixes = (">",)
    runtime_markers = ("antigravity cli",)
    status_fragments = ("? for shortcuts",)
    # The empty input's grey placeholder names the mode shift+tab cycles
    # through; narrow panes cut it short with "..." (1.5.5, live).
    placeholder_fragments = (
        "auto mode: edits & safe commands",
        "accept-edits mode: file edits",
        "plan mode: research & plan only",
        "best-of-n mode: fork into parallel",
    )
    # "⣾  Generating..." above the input, "esc to cancel" in the status line.
    busy_markers = ("esc to cancel", "generating...")
    # The heading, and the footer under the options, which stays on screen
    # when a long command pushes the heading up.
    prompt_markers = ("requesting permission for:", "run this command?", "· tab amend ·")
    # "> 1. Yes, run command": "1" runs it wherever the cursor is (live).
    approve_keys = ("1",)
    # "--dangerously-skip-permissions  Auto-approve all tool permission
    # requests without prompting" (agy --help). No OS sandbox behind it.
    unattended_args = ("--dangerously-skip-permissions",)

    def detect_busy(self, pane_content: str) -> bool:
        # A permission dialog keeps "esc to cancel" in the status line (live):
        # the agent is waiting for a person there, not working.
        return super().detect_busy(pane_content) and not self.detect_active_dialog(pane_content)

    def effort_args(self, effort: str | None) -> list[str]:
        return ["--effort", effort] if effort else []

    def pre_trust(self, directory: Path | str, *, env: dict[str, str] | None = None) -> None:
        env = self.environment(env)
        settings = agent_home(env) / ".gemini" / "antigravity-cli" / "settings.json"
        pre_trust_workspace(directory, self.check_native_path(settings))

    def launch_args(self, *, model, resume, brief_file, pre_trust, data_dir, state_dir):
        args: list[str] = []
        if model:
            args.extend(["--model", model])
        if resume:
            args.append("--continue")  # the most recent conversation
        if brief_file is not None and (brief := read_brief(brief_file)):
            args.extend(["--prompt-interactive", brief])
        return args


RUNTIME = Antigravity()
