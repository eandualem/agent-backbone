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


class Codex(Runtime):
    id = "codex"
    display_name = "Codex"
    binary = "codex"
    brief_mode = "initial_prompt"

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
    # "› 1. Yes, proceed (y)" is preselected; "Press enter to confirm" (0.152).
    approve_keys = ("Enter",)
    interrupt_queued_delivery = True

    def pre_trust(self, directory: Path | str) -> None:
        pre_trust_codex_directory(directory)

    def launch_args(self, *, model, resume, brief_file, pre_trust, data_dir, state_dir):
        # `codex resume` is a subcommand; the resumed session keeps its model.
        if resume:
            return ["resume", "--last"]
        args: list[str] = []
        if model:
            args.extend(["--model", model])
        if brief_file is not None and (brief := read_brief(brief_file)):
            args.append(brief)  # positional initial prompt
        return args


RUNTIME = Codex()
