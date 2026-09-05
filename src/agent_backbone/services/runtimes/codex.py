"""OpenAI Codex CLI. Markers verified live against codex-cli 0.152."""

from __future__ import annotations

import json
import logging
import tomllib
from pathlib import Path

from agent_backbone.fs import atomic_write_text
from agent_backbone.services.runtimes.base import Runtime, read_brief

log = logging.getLogger(__name__)


def pre_trust_codex_directory(directory: Path | str, *, codex_config: Path | None = None) -> bool:
    """Mark a directory as trusted in Codex's ``~/.codex/config.toml``.

    Writes the same record Codex's own trust dialog writes
    (``[projects."<dir>"] trust_level = "trusted"``). A directory that already
    has any ``projects`` entry is left untouched — the user decided. The write
    is best-effort: on any error the dialog simply appears as before. The
    read-modify-write is not locked against Codex itself (which has no writer
    protocol to join); the window is a few milliseconds at agent start.
    """
    path = str(Path(directory).expanduser().resolve())
    config_file = codex_config or (Path.home() / ".codex" / "config.toml")
    try:
        raw = config_file.read_text() if config_file.is_file() else ""
        data = tomllib.loads(raw)
        projects = data.get("projects")
        existing = projects.get(path) if isinstance(projects, dict) else None
        if existing is not None:
            # Valid TOML with an unexpected shape is the user's; leave it alone.
            return isinstance(existing, dict) and existing.get("trust_level") == "trusted"
        # json.dumps yields a TOML basic string: quotes and backslashes in the
        # directory name cannot open another table or change the key.
        entry = f'\n[projects.{json.dumps(path)}]\ntrust_level = "trusted"\n'
        updated = raw.rstrip("\n") + "\n" + entry if raw else entry.lstrip("\n")
        tomllib.loads(updated)  # never leave codex an unparseable config
        atomic_write_text(config_file, updated)
        log.info("Pre-trusted %s for Codex", path)
        return True
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        log.warning("Could not pre-trust %s for Codex (the trust dialog will appear)", path)
        return False


def _toml_string(value: str) -> str:
    # A JSON string is a valid TOML basic string for the escapes json emits.
    return json.dumps(value)


def _toml_entries(entries: list[dict]) -> str:
    """The TOML inline form of one event's hook entries."""
    parts = []
    for entry in entries:
        hooks = ", ".join(
            f"{{type = {_toml_string(h['type'])}, command = {_toml_string(h['command'])}, "
            f"timeout = {int(h['timeout'])}}}"
            for h in entry["hooks"]
        )
        fields = []
        if "matcher" in entry:
            fields.append(f"matcher = {_toml_string(entry['matcher'])}")
        fields.append(f"hooks = [{hooks}]")
        parts.append("{" + ", ".join(fields) + "}")
    return "[" + ", ".join(parts) + "]"


# Codex's workspace-write sandbox has no network, so `backbone tell` from a
# member cannot reach the backbone API on 127.0.0.1 — every message to a peer
# fails, Codex escalates, and a person has to answer a dialog per message.
# This override lets the sandbox reach the network; verified live against
# codex-cli 0.153 (API probe: 000 without it, 401 with it).
_LOCAL_API_ACCESS = ("-c", "sandbox_workspace_write.network_access=true")


class Codex(Runtime):
    id = "codex"
    display_name = "Codex"
    binary = "codex"
    brief_mode = "initial_prompt"
    models = ("gpt-5.6-sol", "gpt-6-astra")  # as shown by codex's own status line (live capture)
    # Codex has no effort flag; the level is a config override. Levels as
    # gpt-6-astra reports them in codex 0.153 (`~/.codex/models_cache.json`).
    efforts = ("low", "medium", "high", "xhigh", "max", "ultra")

    hook_script = "codex_hook.py"
    hook_events = (
        ("SessionStart", None),
        ("SessionEnd", None),
        ("UserPromptSubmit", None),
        ("PermissionRequest", None),
        ("PreToolUse", None),
        ("PostToolUse", None),
        ("Stop", None),
        ("Interrupt", None),
    )
    hook_timeout = 10  # seconds

    prompt_prefixes = ("›",)
    runtime_markers = ("openai codex", "gpt-5.", "context left")
    placeholder_fragments = (
        "ask codex to do anything",
        "implement {feature}",
        "explain this codebase",
    )
    status_fragments = (
        "gpt-5.",
        "context left",
        "for shortcuts",
        "messages to be submitted after next tool call",
    )
    queue_markers = (
        "tab to queue message",
        "messages to be submitted after next tool call",
        "press esc to interrupt and send immediately",
    )
    busy_markers = ("esc to interrupt",)
    prompt_markers = (
        "approve this command",
        "allow command",
        "would you like to run the following command",
        "yes, and don't ask again",
        "do you trust the contents of this directory",
        "press enter to continue",
        "press enter to confirm",
    )
    # "› 1. Yes, proceed (y)" is preselected; "Press enter to confirm" (0.152);
    # "3. No, and tell Codex what to do differently (esc)".
    approve_keys = ("Enter",)
    deny_keys = ("Escape",)
    interrupt_queued_delivery = True

    def pre_trust(self, directory: Path | str) -> None:
        pre_trust_codex_directory(directory)

    def effort_args(self, effort: str | None) -> list[str]:
        """``-c model_reasoning_effort=<level>``, Codex's config override.

        A global option, so it is valid both before the TUI and before the
        ``resume`` subcommand. Verified live against codex-cli 0.153.
        """
        return ["-c", f"model_reasoning_effort={effort}"] if effort else []

    def hook_settings_path(self, project_dir: Path | None) -> Path:
        # Codex reads `hooks.json` from its home and from a trusted project's
        # `.codex/`; entries there still need a one-time `/hooks` trust.
        if project_dir is not None:
            return Path(project_dir).expanduser() / ".codex" / "hooks.json"
        return Path.home() / ".codex" / "hooks.json"

    def hook_launch_args(
        self, data_dir: Path | str | None, state_dir: Path | str | None
    ) -> list[str]:
        """``-c hooks.<Event>=[…]`` per event, plus ``--dangerously-bypass-hook-trust``.

        Codex takes configuration overrides on the command line (dotted keys,
        TOML values), so the hooks live only in this launch: nothing in
        ``~/.codex`` or the repository is touched. Codex asks a person to trust
        any hook it has not seen; these are the backbone's own scripts, wired
        by the backbone, so the trust prompt is bypassed for this session.
        Verified live against codex-cli 0.152.
        """
        if data_dir is None or state_dir is None:
            return []
        try:
            _, settings = self.hook_settings(data_dir, state_dir)
        except OSError as exc:
            log.warning("Could not write the hook files: %s", exc)
            return []
        args: list[str] = []
        for event, entries in settings["hooks"].items():
            args.extend(["-c", f"hooks.{event}={_toml_entries(entries)}"])
        args.append("--dangerously-bypass-hook-trust")
        return args

    def launch_args(self, *, model, resume, brief_file, pre_trust, data_dir, state_dir):
        hook = self.hook_launch_args(data_dir, state_dir)
        # `codex resume` is a subcommand; the resumed session keeps its model.
        # Both the TUI and `resume` take `-c` and the hook-trust flag.
        if resume:
            target = resume if isinstance(resume, str) else "--last"
            return ["resume", target, *_LOCAL_API_ACCESS, *hook]
        args: list[str] = [*_LOCAL_API_ACCESS, *hook]
        if model:
            args.extend(["--model", model])
        if brief_file is not None and (brief := read_brief(brief_file)):
            args.append(brief)  # positional initial prompt, after every flag
        return args


RUNTIME = Codex()
