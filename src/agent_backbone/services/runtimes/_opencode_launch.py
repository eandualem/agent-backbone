"""Compose OpenCode hooks with the environment actually inherited at exec time.

Executed as a standalone standard-library-only launcher before the runtime.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def merge_config(raw: str | None, plugin: str) -> str | None:
    """Return merged JSON, or None to preserve an unsupported config verbatim."""
    try:
        content = json.loads(raw or "{}")
    except ValueError:
        return None
    if not isinstance(content, dict) or not isinstance(content.get("plugin", []), list):
        return None
    plugins = content.setdefault("plugin", [])
    if plugin not in plugins:
        plugins.append(plugin)
    return json.dumps(content)


def main() -> None:
    plugin, *command = sys.argv[1:]
    content = merge_config(os.environ.get("OPENCODE_CONFIG_CONTENT"), Path(plugin).as_uri())
    if content is None:
        print(
            "Skipping OpenCode hook injection: preserving unsupported inline configuration",
            file=sys.stderr,
        )
    else:
        os.environ["OPENCODE_CONFIG_CONTENT"] = content
    os.execv(command[0], command)


if __name__ == "__main__":
    main()
