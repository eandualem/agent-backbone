"""The files and settings shapes behind every runtime's hooks.

The hook scripts are copied into ``<data_dir>/hooks/`` so an agent never
needs the backbone's virtualenv; ``hook_command`` builds the command a CLI
runs for each event; ``merge_hooks`` / ``remove_hooks`` edit the
``{"hooks": {"<Event>": [{"matcher": …, "hooks": [{"type": "command",
"command": …}]}]}}`` shape that Claude Code, Codex and Gemini CLI all use,
tagging every entry so re-installs and uninstalls are idempotent. Which
events a runtime listens to, and where its settings live, is that
runtime's own business (``services/runtimes/<cli>.py``).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

from agent_backbone.config import RUNTIME_METADATA
from agent_backbone.fs import atomic_write_text

HOOK_MARKER = "agent-backbone"

HOOK_FILES = (
    "backbone_state.py",
    *(entry["hook_script"] for entry in RUNTIME_METADATA if entry["hook_script"]),
)
"""Everything ``<data_dir>/hooks/`` receives; the runtime names its own script."""

Events = tuple[tuple[str, str | None], ...]
"""``(event, matcher)`` pairs; ``None`` omits the matcher, ``""`` means every tool."""


def hook_source(name: str) -> Path:
    return Path(__file__).with_name(name)


def install_hook_files(data_dir: Path) -> Path:
    """Copy the standard-library-only hook files into ``<data_dir>/hooks/``."""
    hooks_dir = data_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    for name in HOOK_FILES:
        target = hooks_dir / name
        # Every agent launch re-installs; a running agent's hook may fire
        # mid-copy, so the live file is only ever replaced whole.
        fd, tmp_name = tempfile.mkstemp(dir=hooks_dir, prefix=f".{name}.")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as out, open(hook_source(name), "rb") as src:
                shutil.copyfileobj(src, out)
            if name.endswith(".py"):
                tmp.chmod(0o755)
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    return hooks_dir


_TAG_ARG = f"--tag {HOOK_MARKER}"


def hook_command(script: Path, state_dir: Path, python: str | None = None) -> str:
    """Shell command a CLI runs for each event (tagged so we can find it again)."""
    interpreter = python or "python3"
    quoted = shlex.join([interpreter, str(script), "--state-dir", str(state_dir)])
    return f"{quoted} {_TAG_ARG}"


def is_ours(entry: dict) -> bool:
    return any(
        isinstance(h, dict) and h.get("type") == "command" and _TAG_ARG in h.get("command", "")
        for h in entry.get("hooks", [])
    )


def hook_entry(command: str, matcher: str | None, timeout: int) -> dict:
    entry: dict = {"hooks": [{"type": "command", "command": command, "timeout": timeout}]}
    if matcher is not None:
        entry["matcher"] = matcher
    return entry


def merge_hooks(settings: dict, events: Events, command: str, timeout: int) -> dict:
    """Return settings with backbone hook entries added (replacing older ones)."""
    hooks = dict(settings.get("hooks") or {})
    for event, matcher in events:
        entries = [e for e in hooks.get(event, []) if not (isinstance(e, dict) and is_ours(e))]
        entries.append(hook_entry(command, matcher, timeout))
        hooks[event] = entries
    return {**settings, "hooks": hooks}


def remove_hooks(settings: dict) -> dict:
    """Return settings with every backbone hook entry removed."""
    hooks = {}
    for event, entries in (settings.get("hooks") or {}).items():
        kept = [e for e in entries if not (isinstance(e, dict) and is_ours(e))]
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
    atomic_write_text(path, json.dumps(settings, indent=2) + "\n")


def default_python() -> str:
    """Interpreter for the hook command: the current one if it is a plain path."""
    exe = Path(sys.executable)
    return str(exe) if exe.is_file() else "python3"


CHROME_HOST = "com.agent_backbone.tab_names"
CHROME_EXTENSION_ID = "apmflpbnkiglbnppfajmimbjebjigagh"
"""Fixed by the public ``key`` in ``chrome_tab_names/manifest.json``: native
messaging admits only the extension IDs its manifest lists."""


def chrome_host_manifest() -> Path:
    """Where Google Chrome looks up a user's native messaging hosts."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    else:
        base = Path.home() / ".config" / "google-chrome"
    return base / "NativeMessagingHosts" / f"{CHROME_HOST}.json"


def install_chrome_tab_names(
    data_dir: Path, state_dir: Path, python: str | None = None
) -> tuple[Path, Path]:
    """Copy the tab-names extension and its native host into
    ``<data_dir>/chrome-tab-names/`` and register the host with Chrome.
    Returns (extension folder to load unpacked, host manifest)."""
    target = data_dir / "chrome-tab-names"
    shutil.copytree(hook_source("chrome_tab_names"), target / "extension", dirs_exist_ok=True)
    host = target / "host.py"
    shutil.copy2(hook_source("chrome_tab_names_host.py"), host)
    wrapper = target / "host"
    command = shlex.join([python or default_python(), str(host), "--state-dir", str(state_dir)])
    atomic_write_text(wrapper, f'#!/bin/sh\nexec {command} "$@"\n')
    wrapper.chmod(0o755)
    manifest = chrome_host_manifest()
    manifest.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        manifest,
        json.dumps(
            {
                "name": CHROME_HOST,
                "description": "Agent Backbone tab group names",
                "path": str(wrapper),
                "type": "stdio",
                "allowed_origins": [f"chrome-extension://{CHROME_EXTENSION_ID}/"],
            },
            indent=2,
        ),
    )
    return target / "extension", manifest


def uninstall_chrome_tab_names(data_dir: Path) -> list[Path]:
    """Remove the host registration and the copied files; returns what was removed."""
    removed = []
    manifest = chrome_host_manifest()
    if manifest.exists():
        manifest.unlink()
        removed.append(manifest)
    target = data_dir / "chrome-tab-names"
    if target.exists():
        shutil.rmtree(target)
        removed.append(target)
    return removed
