"""Plain shells: classic ``$``/``%`` prompts and modern ``❯``/``›`` themes.

Exists for testing the plumbing; the ``[via:…]`` envelope is a glob to a
shell, so nothing briefs it.
"""

from __future__ import annotations

from agent_backbone.services.runtimes.base import Runtime


class Shell(Runtime):
    id = "shell"
    display_name = "Plain shell"
    binary = None
    brief_mode = "none"

    prompt_prefixes = ("❯", "›", "$ ", "% ", "> ", "# ")
    prompt_suffixes = ("$", "%", ">", "#", "❯", "›")


class Unknown(Shell):
    """A pane no runtime claims: read like a shell, never launched."""

    id = "unknown"
    display_name = "Unknown"


RUNTIME = Shell()
UNKNOWN = Unknown()
