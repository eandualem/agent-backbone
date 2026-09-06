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
    model_tags = True

    runtime_markers = ("opencode", "ask anything...", "tab agents")
    placeholder_fragments = ("ask anything...", "ctrl+p commands")
    status_fragments = ("tab agents", "ctrl+p commands")
    busy_markers = ("esc interrupt",)
    provider_error_patterns = (
        r"^(?:error:\s*)?you exceeded your current quota\b",
        r"^(?:error:\s*)?(?:rate limit (?:reached|exceeded)|resource_exhausted)\b",
        r"^(?:error:\s*)?selected model is at capacity\b",
    )
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
        self,
        data_dir: Path | str | None,
        state_dir: Path | str | None,
        *,
        env: dict[str, str] | None = None,
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
        # Inline config has higher precedence than the user's files. Preserve
        # its provider options, permission denies and existing plugins. If we
        # cannot compose it (e.g. JSONC), leave it intact for OpenCode to read
        # and use terminal state detection instead of dropping configuration.
        try:
            content = json.loads((env or {}).get("OPENCODE_CONFIG_CONTENT") or "{}")
        except ValueError:
            content = None
        if not isinstance(content, dict) or not isinstance(content.get("plugin", []), list):
            log.warning(
                "Skipping OpenCode hook injection: OPENCODE_CONFIG_CONTENT must be a JSON "
                "object with a plugin array to merge; leaving the existing configuration intact"
            )
            return {}
        try:
            plugin = hooks.install_hook_files(Path(data_dir)) / self.hook_script
        except OSError as exc:
            log.warning("Could not write the hook files: %s", exc)
            return {}
        plugins = content.setdefault("plugin", [])
        if plugin.as_uri() not in plugins:
            plugins.append(plugin.as_uri())
        return {"OPENCODE_CONFIG_CONTENT": json.dumps(content)}

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
