"""Runtime hooks shipped with the backbone.

Hooks run inside the agent's CLI and push the agent's state — idle, busy,
waiting_for_human (with a reason) — into the backbone's state directory,
so delivery decisions do not depend on screen-scraping. One script per
runtime (``claude_hook.py``, ``codex_hook.py``, ``gemini_hook.py``, the
``opencode_hook.js`` plugin) maps that CLI's events onto the shared
vocabulary in ``backbone_state.py``; how each is wired at launch is the
runtime's own business (``services/runtimes/<cli>.py``).

The hook scripts are standard-library-only so they run under whatever
``python3`` the agent's shell finds; they never need the backbone's
virtualenv or API key.
"""
