"""Completion can inspect a partial command without executing it or mutating data."""

from __future__ import annotations

import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from agent_backbone.cli import build_parser, main
from agent_backbone.cli.completion import _SCRIPTS, _install, _rc_path, candidates, complete


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKBONE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("BACKBONE_DATABASE_URL", raising=False)
    with sqlite3.connect(tmp_path / "backbone.db") as conn:
        conn.execute("CREATE TABLE agents (name TEXT, tags TEXT)")
        conn.execute("INSERT INTO agents VALUES ('api', '[\"backend\"]')")
        conn.execute("INSERT INTO agents VALUES ('api-tests', '[]')")
        conn.execute("CREATE TABLE swarms (name TEXT)")
        conn.execute("INSERT INTO swarms VALUES ('audit')")
    return tmp_path


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        (["ag"], ["agent"]),
        (["agent", "st"], ["start", "stop"]),
        (["agent", "start", "ap"], ["api", "api-tests"]),
        (["agent", "start", "api", "ap"], ["api", "api-tests"]),
        (["agent", "start", "--runtime", "co"], ["codex"]),
        (["agent", "start", "--runtime=co"], ["--runtime=codex"]),
        (["agent", "rename", "api", ""], ["--help", "-h"]),
        (["agent", "set", "api", "ru"], ["runtime="]),
        (["swarm", "status", "au"], ["audit"]),
        (["tell", "au"], ["audit"]),
        (["status", "--tag", "ba"], ["backend"]),
        (["config", "get", "agents.tag_p"], ["agents.tag_policy"]),
        (["agent", "start", "--model", "arbitrary"], []),
        (["agent", "set", "api", "runtime=co"], ["runtime=codex"]),
        (["agent", "set", "api", "always_on=f"], ["always_on=false"]),
        (["agent", "start", "--", "--r"], []),
        (["agent", "start", "--missing=ap"], []),
    ],
)
def test_partial_commands(catalog, words, expected):
    assert candidates(build_parser(), words) == expected


def test_missing_database_is_not_created(tmp_path, monkeypatch, capsys):
    data = tmp_path / "absent"
    monkeypatch.setenv("BACKBONE_DATA_DIR", str(data))
    monkeypatch.delenv("BACKBONE_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as exit:
        main(["_complete", "--", "agent", "start", ""])
    assert exit.value.code == 0
    assert "--runtime" in capsys.readouterr().out
    assert not data.exists()


def test_completion_does_not_migrate_or_write_database(catalog):
    path = catalog / "backbone.db"
    before = path.read_bytes()
    assert candidates(build_parser(), ["agent", "attach", "api"]) == ["api", "api-tests"]
    assert path.read_bytes() == before
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE name='settings'").fetchone() is None
        )


def test_no_control_characters_in_suggestions(catalog):
    with sqlite3.connect(catalog / "backbone.db") as conn:
        conn.execute("INSERT INTO agents VALUES (?, '[]')", ("bad\ncommand",))
    assert "bad\ncommand" not in candidates(build_parser(), ["agent", "start", ""])


def test_help_paths_and_instruction_template_names(catalog):
    assert candidates(build_parser(), ["help", "agent", "st"]) == ["start", "stop"]
    assert "agents" in candidates(build_parser(), ["help", "ag"])
    assert "swarm:scout" in candidates(build_parser(), ["instructions", "show", "swarm:"])


def test_empty_word_offers_flags_and_names_and_omits_used_flags(catalog):
    options = candidates(build_parser(), ["status", ""])
    assert {"--watch", "--running", "--tag", "--details", "--json"} <= set(options)
    assert {"api", "api-tests", "--read-only"} <= set(
        candidates(build_parser(), ["agent", "attach", ""])
    )
    assert "--watch" not in candidates(build_parser(), ["status", "--watch", ""])
    assert "--watch" in candidates(build_parser(), ["agent", "start", "--watch", "acme/app", ""])
    assert candidates(build_parser(), ["agent", "start", "--runtime", ""]) == [
        "aider",
        "claude",
        "codex",
        "deepcode",
        "gemini",
        "opencode",
        "shell",
    ]


def test_only_directory_arguments_request_files(catalog, capsys):
    parser = build_parser()
    assert complete(parser, ["agent", "start", "--dir", ""]) == 2
    assert complete(parser, ["agent", "set", "api", "dir="]) == 2
    assert complete(parser, ["completion", "zsh", "--rc-file", ""]) == 3
    assert complete(parser, ["agent", "attach", "missing"]) == 0
    assert complete(parser, ["tell", "api", "free text"]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_install_is_idempotent_backed_up_and_removable(tmp_path, shell):
    path = tmp_path / "rc"
    original = "# user's config\n"
    path.write_text(original)
    changed, backup = _install(shell, path, suggestions=False, remove=False)
    assert changed and backup.read_text() == original
    assert backup.stat().st_mode & 0o777 == 0o600
    installed = path.read_text()
    assert f"backbone completion {shell}" in installed
    assert _install(shell, path, suggestions=False, remove=False) == (False, None)
    assert path.read_text() == installed
    _install(shell, path, suggestions=True, remove=False)
    assert path.read_text().count("# >>> agent-backbone completion >>>") == 1
    _install(shell, path, suggestions=False, remove=True)
    assert path.read_text() == original


def test_install_preserves_symlinks_and_rejects_partial_markers(tmp_path):
    target = tmp_path / "dotfile"
    target.write_text("# config\n")
    link = tmp_path / "rc"
    link.symlink_to(target)
    _install("zsh", link, suggestions=True, remove=False)
    assert link.is_symlink() and "--suggestions" in target.read_text()
    target.write_text("# >>> agent-backbone completion >>>\n# incomplete\n")
    before = target.read_text()
    with pytest.raises(ValueError, match="markers"):
        _install("zsh", link, suggestions=False, remove=False)
    assert target.read_text() == before


def test_install_respects_shell_config_locations(tmp_path, monkeypatch):
    monkeypatch.setenv("ZDOTDIR", str(tmp_path / "zsh"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert _rc_path("zsh") == tmp_path / "zsh" / ".zshrc"
    assert _rc_path("fish") == tmp_path / "config/fish/conf.d/agent-backbone-completion.fish"
    assert _rc_path("bash") == Path.home() / ".bashrc"


@pytest.mark.skipif(not shutil.which("bash"), reason="Bash unavailable")
def test_bash_rejoins_readline_word_breaks_without_changing_global_settings(catalog):
    binary = catalog / "backbone"
    binary.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m agent_backbone.cli "$@"\n')
    binary.chmod(0o700)
    script = (
        _SCRIPTS["bash"]
        + r"""
original_breaks=$COMP_WORDBREAKS
COMP_WORDS=(ab agent start --runtime = co)
COMP_CWORD=5
_backbone_complete
printf '%s\n' "${COMPREPLY[@]}"
COMP_WORDS=(ab instructions show swarm : sc)
COMP_CWORD=5
_backbone_complete
printf '%s\n' "${COMPREPLY[@]}"
[[ $original_breaks == "$COMP_WORDBREAKS" ]]
"""
    )
    result = subprocess.run(
        ["bash", "--norc", "-c", script],
        env={**os.environ, "PATH": str(catalog) + os.pathsep + os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert result.stdout.splitlines() == ["codex", "scout"]


def test_concurrent_install_and_remove_serialize_the_entire_edit(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event

    import agent_backbone.cli.completion as module

    path = tmp_path / "rc"
    path.write_text("# existing config\n")
    original_write = module.atomic_write_text
    writing, release = Event(), Event()

    def pause_first_write(target, content):
        if target == path and not writing.is_set():
            writing.set()
            assert release.wait(5)
        original_write(target, content)

    monkeypatch.setattr(module, "atomic_write_text", pause_first_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_install, "zsh", path, suggestions=False, remove=False)
        assert writing.wait(5)
        second = pool.submit(_install, "zsh", path, suggestions=False, remove=True)
        try:
            with pytest.raises(TimeoutError):
                second.result(timeout=0.1)
        finally:
            release.set()
        assert first.result(timeout=5)[0]
        assert second.result(timeout=5)[0]
    assert path.read_text() == "# existing config\n"
