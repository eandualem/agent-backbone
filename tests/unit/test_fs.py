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


def test_text_is_utf8_even_with_an_ascii_locale_default(tmp_path, monkeypatch):
    fdopen = os.fdopen

    def ascii_default(fd, *args, **kwargs):
        kwargs.setdefault("encoding", "ascii")
        return fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", ascii_default)
    target = tmp_path / "SKILL.md"
    atomic_write_text(target, "café")
    assert target.read_bytes() == b"caf\xc3\xa9"
