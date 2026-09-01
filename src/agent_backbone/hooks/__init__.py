"""Runtime hooks shipped with the backbone.

Hooks run inside the agent's CLI (Claude Code today) and push the agent's
state — idle, busy, waiting_for_human (with a reason) — into the backbone's
state directory, so delivery decisions do not depend on screen-scraping.

The hook scripts are standard-library-only so they run under whatever
``python3`` the agent's shell finds; they never need the backbone's
virtualenv or API key.
"""
