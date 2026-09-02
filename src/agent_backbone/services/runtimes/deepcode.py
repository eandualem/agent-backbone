"""Deep Code — the terminal agent DeepSeek's API docs point to (``@vegamo/deepcode-cli``).

Markers captured live from deepcode 0.3.1: the banner box (``>_ Deep Code
(v0.3.1)`` with Model / Thinking / Reasoning rows), the ``>`` prompt with
its ``Type your message...`` placeholder and the ``enter send · … ctrl+d
exit`` bar. The model is not a CLI flag: it comes from ``MODEL`` in the
environment (or ``~/.deepcode/settings.json``). Busy and permission
markers are still empty — they need a capture from a session with a
DeepSeek API key; until then those states are read from the hook-less
fallbacks (see ``GENERIC_BUSY_FRAGMENTS``) and ``approve`` is refused.
"""

from __future__ import annotations

from agent_backbone.services.runtimes.base import Runtime, read_brief


class DeepCode(Runtime):
    id = "deepcode"
    display_name = "Deep Code"
    aliases = ("deep-code", "deep code", "deepseek")
    binary = "deepcode"
    brief_mode = "initial_prompt"
    models = ("deepseek-v4-flash", "deepseek-v4-pro")

    prompt_prefixes = (">",)
    runtime_markers = ("deep code", "type your message...", "/raw - toggle display mode")
    placeholder_fragments = ("type your message...",)
    status_fragments = (
        "enter send ·",
        "ctrl+d exit",
        "/raw - toggle display mode",
        "reasoning effort",
        "thinking enabled",
    )
    # busy_markers / prompt_markers / approve_keys: pending a live capture
    # with an API key (README's Deep Code note).

    def launch_env(self, model: str | None) -> dict[str, str]:
        return {"MODEL": model} if model else {}

    def launch_args(self, *, model, resume, brief_file, pre_trust, data_dir, state_dir):
        args: list[str] = []
        if resume:
            args.append("--last")  # resume the most recent session in this directory
        if brief_file is not None and (brief := read_brief(brief_file)):
            args.extend(["-p", brief])  # launch the TUI and submit the brief
        return args


RUNTIME = DeepCode()
