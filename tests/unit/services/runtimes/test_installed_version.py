"""The installed CLI's version, which `doctor` compares with the verified one (#359)."""

import pytest

from agent_backbone.services.runtimes import RUNTIMES, base

# What each CLI's ``--version`` printed, checked live, and the version read from it.
PRINTED = {
    "claude": ("2.1.284 (Claude Code)", "2.1.284"),
    "codex": ("codex-cli 0.157.1", "0.157.1"),
    "gemini": ("0.46.0", "0.46.0"),
    "opencode": ("1.18.32", "1.18.32"),
    "deepcode": ("0.3.1", "0.3.1"),
}


@pytest.fixture
def bin_dir(tmp_path, monkeypatch):
    """A PATH holding only the stand-in CLIs a test writes."""
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(base, "_FALLBACK_DIRS", ())
    return tmp_path


def _cli(bin_dir, name, body):
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


@pytest.mark.parametrize("runtime", sorted(PRINTED))
def test_reads_the_version_each_cli_prints(bin_dir, runtime):
    printed, version = PRINTED[runtime]
    _cli(bin_dir, RUNTIMES[runtime].binary, f'[ "$1" = --version ] && echo "{printed}"')
    assert RUNTIMES[runtime].installed_version() == version


@pytest.mark.parametrize(
    ("body", "version"),
    [
        ("echo 'codex-cli 0.157.1-alpha.1'", "0.157.1-alpha.1"),
        ("echo 'codex-cli v0.157.1'", "0.157.1"),
        ("printf '\\377' >&2; echo 'codex-cli 0.157.1'", "0.157.1"),
    ],
    ids=["prerelease kept", "leading v", "undecodable stderr"],
)
def test_reads_a_version_in_other_forms(bin_dir, body, version):
    _cli(bin_dir, "codex", body)
    assert RUNTIMES["codex"].installed_version() == version


@pytest.mark.parametrize(
    "body",
    ["echo 'codex-cli 0.157.1'; exit 1", "echo 'unknown option --version'", "echo 'build 1.2.3.4'"],
    ids=["failing call", "no version printed", "not a version of its own"],
)
def test_a_version_that_cannot_be_read_is_none(bin_dir, body):
    _cli(bin_dir, "codex", body)
    assert RUNTIMES["codex"].installed_version() is None


def test_a_missing_cli_has_no_version(bin_dir):
    assert RUNTIMES["claude"].installed_version() is None
    assert RUNTIMES["shell"].installed_version() is None
