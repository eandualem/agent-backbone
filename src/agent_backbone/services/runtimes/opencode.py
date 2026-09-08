"""OpenCode. Markers verified live against opencode 1.18.

The "Ask anything..." placeholder disappears after the first message, but
the bottom bar ("ctrl+p commands") is always visible, so idle is that bar
without the working spinner's "esc interrupt".
"""

from __future__ import annotations

import logging
from pathlib import Path

from agent_backbone.hooks import install as hooks
from agent_backbone.services.runtimes._opencode_launch import merge_config
from agent_backbone.services.runtimes.base import Runtime, read_brief

log = logging.getLogger(__name__)


class OpenCode(Runtime):
    supports_exact_resume = True
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
        # When no explicit value is supplied, only the launched process knows
        # the effective tmux-server environment. Its wrapper composes it there.
        if env is None or "OPENCODE_CONFIG_CONTENT" not in env:
            return {}
        try:
            plugin = hooks.install_hook_files(Path(data_dir)) / self.hook_script
        except OSError as exc:
            log.warning("Could not write the hook files: %s", exc)
            return {}
        content = merge_config(env["OPENCODE_CONFIG_CONTENT"], plugin.as_uri())
        if content is None:
            log.warning(
                "Skipping OpenCode hook injection: preserving unsupported inline configuration"
            )
            return {}
        return {"OPENCODE_CONFIG_CONTENT": content}

    def build_command(self, **kwargs):
        command = super().build_command(**kwargs)
        data_dir, state_dir = kwargs.get("data_dir"), kwargs.get("state_dir")
        if command is None or data_dir is None or state_dir is None:
            return command
        try:
            plugin = hooks.install_hook_files(Path(data_dir)) / self.hook_script
        except OSError as exc:
            log.warning("Could not install OpenCode launch hooks: %s", exc)
            return command
        return [
            hooks.default_python(),
            str(Path(__file__).with_name("_opencode_launch.py")),
            str(plugin),
            *command,
        ]

    def launch_args(self, *, model, resume, brief_file, pre_trust, data_dir, state_dir):
        args: list[str] = []
        if model:
            args.extend(["--model", model])
        if resume:
            args.extend(["--session", resume] if isinstance(resume, str) else ["--continue"])
        if brief_file is not None and (brief := read_brief(brief_file)):
            args.extend(["--prompt", brief])
        return args


RUNTIME = OpenCode()
