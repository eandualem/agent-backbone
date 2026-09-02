"""Tests for atomic writes."""

from __future__ import annotations

import os
import stat

from agent_backbone.fs import atomic_write_text


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_new_file_is_private(tmp_path):
    old_umask = os.umask(0o000)  # a permissive umask must not leak the file
    try:
        target = tmp_path / "state.json"
        atomic_write_text(target, "{}")
    finally:
        os.umask(old_umask)
    assert target.read_text() == "{}"
    assert _mode(target) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]  # no temp file left


def test_existing_mode_is_kept(tmp_path):
    target = tmp_path / "claude.json"
    target.write_text("{}")
    target.chmod(0o644)
    atomic_write_text(target, '{"a": 1}')
    assert target.read_text() == '{"a": 1}'
    assert _mode(target) == 0o644
