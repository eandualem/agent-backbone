"""Compose OpenCode hooks with the environment actually inherited at exec time.

Executed as a standalone standard-library-only launcher before the runtime.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def merge_config(raw: str | None, plugin: str, *, v2: bool = False) -> str | None:
    """Return merged JSON, or None to preserve an unsupported config verbatim."""
    try:
        content = json.loads(raw or "{}")
    except ValueError:
        return None
    key = "plugins" if v2 else "plugin"
    if not isinstance(content, dict) or not isinstance(content.get(key, []), list):
        return None
    plugins = content.setdefault(key, [])
    if plugin not in plugins:
        plugins.append(plugin)
    return json.dumps(content)


def main() -> None:
    args = sys.argv[1:]
    v2 = args[0] == "--v2"
    plugin, *command = args[1:] if v2 else args
    content = merge_config(
        os.environ.get("OPENCODE_CONFIG_CONTENT"),
        str(Path(plugin).resolve()) if v2 else Path(plugin).as_uri(),
        v2=v2,
    )
    if content is None:
        if v2:
            raise SystemExit(
                "Cannot inject OpenCode v2 hooks into unsupported inline configuration; "
                "use a JSON object with a plugins list"
            )
        print(
            "Skipping OpenCode hook injection: preserving unsupported inline configuration",
            file=sys.stderr,
        )
    else:
        os.environ["OPENCODE_CONFIG_CONTENT"] = content
    os.execv(command[0], command)


if __name__ == "__main__":
    main()
