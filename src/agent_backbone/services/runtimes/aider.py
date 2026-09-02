"""Aider. No launch-time brief; it arrives as the first delivered message."""

from __future__ import annotations

from agent_backbone.services.runtimes.base import Runtime


class Aider(Runtime):
    id = "aider"
    display_name = "Aider"
    aliases = ("aider-chat",)
    binary = "aider"
    brief_mode = "message"

    prompt_prefixes = ("aider>", ">")
    # "model:" and "/help" appear in other CLIs' chrome; only aider's own banner
    # and prompt identify it.
    runtime_markers = ("aider v", "aider>", "aider chat")
    status_fragments = ("tokens:", "cost:")
    prompt_markers = ("(y)es/(n)o", "[y/n]", "(y/n)")
    # approve_keys stays empty until aider's prompt is captured live.


RUNTIME = Aider()
