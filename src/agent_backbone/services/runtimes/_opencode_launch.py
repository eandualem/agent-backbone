"""Compose OpenCode hooks with the environment actually inherited at exec time.

Executed as a standalone standard-library-only launcher before the runtime.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

if __package__:
    from ._opencode_v2_launch import record_failure
else:
    from _opencode_v2_launch import record_failure


def merge_config(raw: str | None, plugin: str) -> str | None:
    """Return merged JSON, or None to preserve an unsupported config verbatim."""
    try:
        content = json.loads(raw or "{}")
    except ValueError:
        return None
    key = "plugins"
    if not isinstance(content, dict) or not isinstance(content.get(key, []), list):
        return None
    plugins = content.setdefault(key, [])
    if plugin not in plugins:
        plugins.append(plugin)
    return json.dumps(content)


def main() -> None:
    args = sys.argv[1:]
    plugin, *command = args
    content = merge_config(
        os.environ.get("OPENCODE_CONFIG_CONTENT"),
        str(Path(plugin).resolve()),
    )
    if content is None:
        error = (
            "Cannot inject OpenCode hooks into unsupported inline configuration; "
            "use a JSON object with a plugins list"
        )
        record_failure(error)
        raise SystemExit(error)
    os.environ["OPENCODE_CONFIG_CONTENT"] = content
    os.execv(command[0], command)


if __name__ == "__main__":
    main()
