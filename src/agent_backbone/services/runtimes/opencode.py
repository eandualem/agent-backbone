"""OpenCode. Markers verified live against opencode 1.18.

The "Ask anything..." placeholder disappears after the first message, but
the bottom bar ("ctrl+p commands") is always visible, so idle is that bar
without the working spinner's "esc interrupt".
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from agent_backbone.hooks import install as hooks
from agent_backbone.services.runtimes.base import Runtime, read_brief

log = logging.getLogger(__name__)


class OpenCode(Runtime):
    id = "opencode"
    display_name = "OpenCode"
    aliases = ("open-code", "open_code")
    binary = "opencode"
    brief_mode = "initial_prompt"

    hook_script = "opencode_hook.js"  # a plugin, not a command hook

    runtime_markers = ("opencode", "ask anything...", "tab agents")
    placeholder_fragments = ("ask anything...", "ctrl+p commands")
    status_fragments = ("tab agents", "ctrl+p commands")
    busy_markers = ("esc interrupt",)
    # "△ Permission required … Allow once  Allow always  Reject … enter confirm"
    # with "Allow once" preselected (live capture, 1.18).
    prompt_markers = ("permission required", "allow once", "allow always")
    approve_keys = ("Enter",)
    # "--auto  auto-approve permissions that are not explicitly denied"
    # (opencode 1.18 TUI); a `permission` deny in the user's config still
    # holds. OpenCode has no OS sandbox: this is trust on the machine.
    unattended_args = ("--auto",)

    def hook_settings(self, data_dir, state_dir, *, python=None):
        raise RuntimeError("OpenCode state comes from a plugin, not a command hook")

    def hook_launch_env(
        self, data_dir: Path | str | None, state_dir: Path | str | None
    ) -> dict[str, str]:
        """``OPENCODE_CONFIG_CONTENT`` → ``{"plugin": ["file://…/opencode_hook.js"]}``.

        OpenCode merges that inline configuration over the user's own, so
        the plugin loads for this session only; nothing in
        ``~/.config/opencode`` or the repository is touched. The plugin reads
        ``BACKBONE_AGENT`` and ``BACKBONE_STATE_DIR`` from the session.
        Verified live against OpenCode 1.18 (TUI).
        """
        if data_dir is None or state_dir is None:
            return {}
        try:
            plugin = hooks.install_hook_files(Path(data_dir)) / self.hook_script
        except OSError as exc:
            log.warning("Could not write the hook files: %s", exc)
            return {}
        return {"OPENCODE_CONFIG_CONTENT": json.dumps({"plugin": [plugin.as_uri()]})}

    def launch_args(self, *, model, resume, brief_file, pre_trust, data_dir, state_dir):
        args: list[str] = []
        if model:
            args.extend(["--model", model])
        if resume:
            args.append("--continue")  # opencode's resume flag
        if brief_file is not None and (brief := read_brief(brief_file)):
            args.extend(["--prompt", brief])
        return args


RUNTIME = OpenCode()
