"""Small filesystem helpers shared across packages."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path


def contained_path(root: Path, relative: str | Path, *, follow_leaf: bool = True) -> Path:
    """Reject traversal and existing symlink redirects outside a chosen root.

    This is a pathname check, not isolation from concurrent same-user renames.
    A symlink being created or removed is checked with ``follow_leaf=False``.
    """
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("path must stay inside its repository")
    root = root.resolve()
    path = root / relative
    checked = path if follow_leaf else path.parent
    if not checked.resolve().is_relative_to(root):
        raise ValueError("path redirects outside its repository")
    return path


def atomic_write_text(path: Path, text: str) -> None:
    """Write through a unique sibling temp file and rename.

    Readers never see a torn file, and concurrent writers (a hook and the
    API updating the same agent) never share a temp name. An existing file
    keeps its mode; a new one is private (0600).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = 0o600
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fd, mode)
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
