"""Claude Code."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from agent_backbone.fs import atomic_write_text
from agent_backbone.hooks.install import save_settings
from agent_backbone.services.runtimes.base import Runtime

log = logging.getLogger(__name__)


def pre_trust_directory(directory: Path | str, *, claude_config: Path | None = None) -> bool:
    """Mark a directory as trusted in Claude Code's per-project state.

    Writes the same record the interactive folder-trust dialog writes
    (``projects.<dir>.hasTrustDialogAccepted`` in ``~/.claude.json``), so a
    backbone-started session reaches its prompt without a human attaching.
    Starting an agent in a directory is a deliberate act by the owner or an
    authorized agent — that decision replaces the dialog. The write is
    best-effort: on any error the dialog simply appears as before.
    """
    path = str(Path(directory).expanduser().resolve())
    config_file = claude_config or (Path.home() / ".claude.json")
    try:
        data = json.loads(config_file.read_text()) if config_file.is_file() else {}
        if not isinstance(data, dict):
            return False
        projects = data.setdefault("projects", {})
        entry = projects.setdefault(path, {})
        if entry.get("hasTrustDialogAccepted") is True:
            return True
        entry["hasTrustDialogAccepted"] = True
        atomic_write_text(config_file, json.dumps(data, indent=2))
        log.info("Pre-trusted %s for Claude Code", path)
        return True
    except (OSError, ValueError):
        log.warning("Could not pre-trust %s (the trust dialog will appear)", path)
        return False


class ClaudeCode(Runtime):
    id = "claude"
    display_name = "Claude Code"
    aliases = ("claude-code", "claude code")
    binary = "claude"
    brief_mode = "system_prompt"
    models = ("opus", "sonnet", "haiku")  # Claude Code's own aliases
    # `claude --effort <level>`; levels as Claude Code itself lists them when
    # it rejects an unknown one (live capture).
    efforts = ("low", "medium", "high", "xhigh", "max")
    # "--dangerously-skip-permissions  Bypass all permission checks." No OS
    # sandbox behind it: trust on the machine. Claude Code asks once per
    # machine to accept bypass mode (see prompt_markers): the backbone shows
    # the dialog and never answers it — a person does, once.
    unattended_args = ("--dangerously-skip-permissions",)

    hook_script = "claude_hook.py"
    # An empty matcher means every tool; None omits the matcher.
    hook_events = (
        ("SessionStart", None),
        ("SessionEnd", None),
        ("UserPromptSubmit", None),
        ("Stop", None),
        ("Notification", None),
        ("PreToolUse", "ExitPlanMode|AskUserQuestion|Bash|mcp__.*__add_issue_comment"),
        ("PostToolUse", ""),
    )
    hook_timeout = 10  # seconds

    prompt_prefixes = ("❯",)
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
        # "WARNING: Claude Code running in Bypass Permissions mode … ❯ No, exit /
        # Yes, I accept" (live capture, 2.1.x, first unattended start). Without
        # this marker its unnumbered "❯ No, exit" reads as an idle prompt with
        # typed text; with it the agent is waiting_for_human. Its options
        # carry no numbers, so it is never an answerable dialog: `agent
        # approve` types nothing (Enter would exit), a person answers it once.
        "bypass permissions mode",
    )
    # "❯ 1. Yes" is preselected in the permission dialog (live capture, 2.1.x);
    # its footer says "Esc to cancel".
    approve_keys = ("Enter",)
    deny_keys = ("Escape",)
    # Plan mode: Shift+Tab (sent as Escape then "[Z") accepts the plan;
    # Escape leaves plan mode so feedback can follow as a message.
    plan_approve_keys = ("Escape", "[Z")
    plan_reject_keys = ("Escape",)

    def pre_trust(self, directory: Path | str) -> None:
        pre_trust_directory(directory)

    def hook_settings_path(self, project_dir: Path | None) -> Path:
        if project_dir is not None:
            return Path(project_dir).expanduser() / ".claude" / "settings.json"
        return Path("~/.claude/settings.json").expanduser()

    def hook_launch_args(
        self, data_dir: Path | str | None, state_dir: Path | str | None
    ) -> list[str]:
        """``--settings <data_dir>/hooks/claude-settings.json``.

        Claude Code accepts an additional settings file; the backbone keeps
        one under ``<data_dir>/hooks/``, regenerated on every start, so every
        session it starts reports state without the user configuring hooks
        per repository (or at all). Nothing outside ``<data_dir>/hooks/`` is
        touched.
        """
        if data_dir is None or state_dir is None:
            return []
        try:
            _, settings = self.hook_settings(data_dir, state_dir)
            path = Path(data_dir) / "hooks" / "claude-settings.json"
            save_settings(path, settings)
        except OSError as exc:
            log.warning("Could not write the launch hook settings: %s", exc)
            return []
        return ["--settings", str(path)]

    def effort_args(self, effort: str | None) -> list[str]:
        """``--effort <level>``, Claude Code's own session flag."""
        return ["--effort", effort] if effort else []

    def launch_args(self, *, model, resume, brief_file, pre_trust, data_dir, state_dir):
        args = super().launch_args(
            model=model,
            resume=resume,
            brief_file=brief_file,
            pre_trust=pre_trust,
            data_dir=data_dir,
            state_dir=state_dir,
        )
        if brief_file is not None:
            args.extend(["--append-system-prompt-file", str(brief_file)])
        args.extend(self.hook_launch_args(data_dir, state_dir))
        return args


RUNTIME = ClaudeCode()
