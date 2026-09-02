"""Small filesystem helpers shared across packages."""

from __future__ import annotations

import os
import time
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """Write through a unique sibling temp file and rename.

    Readers never see a torn file, and concurrent writers (a hook and the
    API updating the same agent) never share a temp name.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)
