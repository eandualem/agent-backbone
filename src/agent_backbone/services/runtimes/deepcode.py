"""Deep Code — the terminal agent DeepSeek's API docs point to (``@vegamo/deepcode-cli``).

Markers captured live from deepcode 0.3.1: the banner box (``>_ Deep Code
(v0.3.1)`` with Model / Thinking / Reasoning rows), the ``>`` prompt with
its ``Type your message...`` placeholder, the ``enter send · … ctrl+d
exit`` bar, and while it works a spinner line ``status: processing ·
<model> <effort>`` with ``press esc to interrupt`` in the footer (a failed
turn leaves ``status: failed · …``). The model is not a CLI flag: it comes
from ``MODEL`` in the environment or ``~/.deepcode/settings.json`` (under
``env``). The permission dialog has not been captured yet, so ``approve``
refuses this runtime until it has.
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
        "status: failed",
    )
    busy_markers = ("status: processing", "press esc to interrupt")
    # prompt_markers / approve_keys: the permission dialog is pending a live
    # capture (README's Deep Code note).

    def _is_status_chrome_line(self, line: str) -> bool:
        # The footer wraps at narrow widths and leaves "exit" alone on a line.
        return super()._is_status_chrome_line(line) or line.strip().lower() == "exit"

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
