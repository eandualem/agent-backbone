"""OpenCode. Markers verified live against opencode 1.18.

The "Ask anything..." placeholder disappears after the first message, but
the bottom bar ("ctrl+p commands") is always visible, so idle is that bar
without the working spinner's "esc interrupt".
"""

from __future__ import annotations

from agent_backbone.services.runtimes.base import Runtime, read_brief


class OpenCode(Runtime):
    id = "opencode"
    display_name = "OpenCode"
    aliases = ("open-code", "open_code")
    binary = "opencode"
    brief_mode = "initial_prompt"

    runtime_markers = ("opencode", "ask anything...", "tab agents")
    placeholder_fragments = ("ask anything...", "ctrl+p commands")
    status_fragments = ("tab agents", "ctrl+p commands")
    busy_markers = ("esc interrupt",)
    # "△ Permission required … Allow once  Allow always  Reject … enter confirm"
    # with "Allow once" preselected (live capture, 1.18).
    prompt_markers = ("permission required", "allow once", "allow always")
    approve_keys = ("Enter",)

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
