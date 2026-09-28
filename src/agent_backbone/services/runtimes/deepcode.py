"""Deep Code — the terminal agent DeepSeek's API docs point to (``@vegamo/deepcode-cli``).

Markers captured live from deepcode 0.3.1: the banner box (``>_ Deep Code
(v0.3.1)`` with Model / Thinking / Reasoning rows), the ``>`` prompt with
its ``Type your message...`` placeholder, the ``enter send · … ctrl+d
exit`` bar, and while it works a spinner line ``status: processing ·
<model> <effort>`` with ``press esc to interrupt`` in the footer (a failed
turn leaves ``status: failed · …``). The model and the effort are not CLI
flags: Deep Code reads them from ``DEEPCODE_``-prefixed variables in its
environment (``DEEPCODE_MODEL``, ``DEEPCODE_REASONING_EFFORT``), which win
over ``~/.deepcode/settings.json``; an unprefixed ``MODEL`` counts only
inside that file's ``env`` block (0.3.1). A permission dialog (live, 0.3.1)
replaces the input box: the spinner line reads ``status: ask_permission``
above a box with ``Permission required 1/1``, the tool and its command,
``Do you want to proceed?`` and ``> 1. Yes`` / ``2. No``.
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_backbone.services.runtimes._pane import (
    is_box_line,
    prompt_tail_line_pairs,
    sanitize_pane_content,
)
from agent_backbone.services.runtimes.base import Runtime, agent_home, has_text, read_brief


class DeepCode(Runtime):
    id = "deepcode"
    display_name = "Deep Code"
    aliases = ("deep-code", "deep code", "deepseek")
    binary = "deepcode"
    brief_mode = "initial_prompt"
    # ./.deepcode/AGENTS.md, else ./AGENTS.md, as a system message (0.3.1).
    declared_capabilities = frozenset({"project-instructions"})
    # Project-level interoperable skills; a symlinked skill directory is
    # listed by /skills and in the model's skill catalog (0.3.1).
    skill_dirs = (".agents/skills",)
    models = ("deepseek-v4-flash", "deepseek-v4-pro")
    # The levels its /model picker offers as "Thinking mode [max|high|low]" (0.3.1).
    efforts = ("low", "high", "max")

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
    # A failed request leaves "status: failed · deepseek-v4-flash max · fail:
    # HTTP 429: Rate Limit Reached [type: …]" (live, 0.3.1, local error
    # endpoint). DeepSeek documents 402 Insufficient Balance, 429 Rate Limit
    # Reached and 503 Server Overloaded.
    provider_error_patterns = (r"^status: failed · .*?· fail: (HTTP (?:402|429|503)\b.*)",)
    prompt_markers = ("permission required", "do you want to proceed?")
    # "1" picks Yes wherever the cursor is (live, 0.3.1); Enter would pick the
    # highlighted option, which a person may have moved to No.
    approve_keys = ("1",)
    # "Esc interrupt": the turn ends and the tool never runs, as Escape does in
    # Claude Code and Codex; "No" would depend on where the cursor is.
    deny_keys = ("Escape",)

    def _is_status_chrome_line(self, line: str) -> bool:
        # The footer wraps at narrow widths and leaves "exit" alone on a line.
        return super()._is_status_chrome_line(line) or line.strip().lower() == "exit"

    def provider_failure(self, pane_content: str) -> str | None:
        """The failure Deep Code's own status line reports above the input box.

        The line starts at the left edge and wraps onto unindented lines in a
        narrow pane; replies and echoed prompts are indented, so a reply
        quoting an error never matches. The next turn replaces the line
        (``status: processing``, then ``status: completed``).
        """
        status, wrapping = "", False
        for line in sanitize_pane_content(pane_content).splitlines()[-25:]:
            text = line.strip()
            if line.startswith("status: "):
                status, wrapping = text, True
            elif wrapping and text and not line[0].isspace() and not is_box_line(text):
                status += " " + text
            else:
                wrapping = False
        for pattern in self.provider_error_patterns:
            if match := re.match(pattern, status):
                return match.group(1)[:500]
        return None

    def detect_prompt(self, pane_content: str) -> str | None:
        """Like the base scan, but the input box wraps: continuation lines are
        indented two spaces and sit between the ``>`` line and the separator."""
        for raw_candidate, candidate in reversed(prompt_tail_line_pairs(pane_content)):
            stripped = candidate.strip()
            if not stripped or is_box_line(stripped) or self._is_status_chrome_line(stripped):
                continue
            if candidate.startswith("  ") and not stripped.startswith(">"):
                continue  # a wrapped continuation of the input box
            if self._matches_prompt_line(stripped):
                return raw_candidate.strip()
            break
        lowered = sanitize_pane_content(pane_content).lower()
        return next((f for f in self.placeholder_fragments if f in lowered), None)

    def effort_args(self, effort: str | None) -> list[str]:
        return []  # no flag: the effort travels in the environment (launch_env)

    def launch_env(self, model: str | None, effort: str | None = None) -> dict[str, str]:
        env = {"DEEPCODE_MODEL": model} if model else {}
        if effort:
            # The effort is sent only in thinking mode, as the picker's
            # "Thinking mode [<effort>]" turns it on.
            env.update(DEEPCODE_REASONING_EFFORT=effort, DEEPCODE_THINKING_ENABLED="true")
        return env

    def user_instructions(self, env, project=None):
        # Read when the project has neither ./.deepcode/AGENTS.md nor ./AGENTS.md (0.3.1).
        own = (".deepcode/AGENTS.md", "AGENTS.md")
        if project is not None and any(has_text(Path(project) / name) for name in own):
            return []
        path = agent_home(env) / ".deepcode" / "AGENTS.md"
        return [path] if has_text(path) else []

    def launch_args(self, *, model, resume, brief_file, pre_trust, data_dir, state_dir):
        args: list[str] = []
        if resume:
            args.append("--last")  # resume the most recent session in this directory
        if brief_file is not None and (brief := read_brief(brief_file)):
            args.extend(["-p", brief])  # launch the TUI and submit the brief
        return args


RUNTIME = DeepCode()
