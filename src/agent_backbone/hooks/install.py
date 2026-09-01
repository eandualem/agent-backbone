"""Install runtime hooks into an agent CLI's settings.

Currently supports Claude Code (``~/.claude/settings.json`` or a project's
``.claude/settings.json``). The hook script is copied into
``<data_dir>/hooks/`` so the agent never needs the backbone's virtualenv,
and every hook entry is tagged with a marker so re-installs and uninstalls
are idempotent.
"""

from __future__ import annotations

import json
import shlex
import shutil
import sys
from pathlib import Path

HOOK_MARKER = "agent-backbone"
CLAUDE_SETTINGS_GLOBAL = Path("~/.claude/settings.json")

# (event, matcher) pairs the Claude hook listens to. An empty matcher means
# "all" for tool events and is omitted for the others.
CLAUDE_EVENTS: tuple[tuple[str, str | None], ...] = (
    ("SessionStart", None),
    ("SessionEnd", None),
    ("UserPromptSubmit", None),
    ("Stop", None),
    ("Notification", None),
    ("PreToolUse", "ExitPlanMode|AskUserQuestion"),
    ("PostToolUse", ""),
)


def hook_script_source() -> Path:
    return Path(__file__).with_name("claude_hook.py")


def install_hook_script(data_dir: Path) -> Path:
    """Copy the stdlib-only hook script into the data dir and return its path."""
    hooks_dir = data_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    target = hooks_dir / "claude_hook.py"
    shutil.copyfile(hook_script_source(), target)
    target.chmod(0o755)
    return target


_TAG_ARG = f"--tag {HOOK_MARKER}"


def hook_command(script: Path, state_dir: Path, python: str | None = None) -> str:
    """Shell command Claude Code runs for each event (tagged so we can find it again)."""
    interpreter = python or "python3"
    quoted = shlex.join([interpreter, str(script), "--state-dir", str(state_dir)])
    return f"{quoted} {_TAG_ARG}"


def _is_ours(entry: dict) -> bool:
    return any(
        isinstance(h, dict) and h.get("type") == "command" and _TAG_ARG in h.get("command", "")
        for h in entry.get("hooks", [])
    )


def _hook_entry(command: str, matcher: str | None) -> dict:
    entry: dict = {"hooks": [{"type": "command", "command": command, "timeout": 10}]}
    if matcher is not None:
        entry["matcher"] = matcher
    return entry


def merge_claude_hooks(settings: dict, command: str) -> dict:
    """Return settings with backbone hook entries added (replacing older ones)."""
    hooks = dict(settings.get("hooks") or {})
    for event, matcher in CLAUDE_EVENTS:
        entries = [e for e in hooks.get(event, []) if not (isinstance(e, dict) and _is_ours(e))]
        entries.append(_hook_entry(command, matcher))
        hooks[event] = entries
    return {**settings, "hooks": hooks}


def remove_claude_hooks(settings: dict) -> dict:
    """Return settings with every backbone hook entry removed."""
    hooks = {}
    for event, entries in (settings.get("hooks") or {}).items():
        kept = [e for e in entries if not (isinstance(e, dict) and _is_ours(e))]
        if kept:
            hooks[event] = kept
    result = dict(settings)
    if hooks:
        result["hooks"] = hooks
    else:
        result.pop("hooks", None)
    return result


def load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text() or "{}")
    except ValueError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def save_settings(path: Path, settings: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")


def claude_settings_path(project_dir: Path | None) -> Path:
    if project_dir is not None:
        return project_dir.expanduser() / ".claude" / "settings.json"
    return CLAUDE_SETTINGS_GLOBAL.expanduser()


def ensure_launch_settings(data_dir: Path, state_dir: Path, *, python: str | None = None) -> Path:
    """Backbone-owned Claude settings file passed at launch via ``--settings``.

    Regenerated on every agent start so the hook script and command stay
    current. Nothing outside ``<data_dir>/hooks/`` is touched — the user's
    ``~/.claude/settings.json`` and per-project settings are left alone.
    """
    script = install_hook_script(data_dir)
    command = hook_command(script, state_dir, python=python or default_python())
    path = data_dir / "hooks" / "claude-settings.json"
    save_settings(path, merge_claude_hooks({}, command))
    return path


def install_claude(
    data_dir: Path,
    state_dir: Path,
    *,
    project_dir: Path | None = None,
    python: str | None = None,
) -> tuple[Path, str]:
    """Install the Claude Code hooks. Returns (settings_path, command)."""
    script = install_hook_script(data_dir)
    command = hook_command(script, state_dir, python=python)
    settings_path = claude_settings_path(project_dir)
    settings = load_settings(settings_path)
    save_settings(settings_path, merge_claude_hooks(settings, command))
    return settings_path, command


def uninstall_claude(*, project_dir: Path | None = None) -> Path:
    settings_path = claude_settings_path(project_dir)
    settings = load_settings(settings_path)
    save_settings(settings_path, remove_claude_hooks(settings))
    return settings_path


def default_python() -> str:
    """Interpreter for the hook command: the current one if it is a plain path."""
    exe = Path(sys.executable)
    return str(exe) if exe.is_file() else "python3"
