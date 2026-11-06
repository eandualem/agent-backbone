"""A captured native context cannot drift to ambient or sibling account stores."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_backbone.services.runtimes import RUNTIMES, _environment, install_native_environments


@pytest.fixture(autouse=True)
def context(monkeypatch):
    monkeypatch.setattr(_environment, "_CONTEXT", None)
    monkeypatch.setattr(
        "agent_backbone.services.runtimes.base.resolve_command", lambda _: "/bin/echo"
    )


@pytest.mark.parametrize("runtime", ["claude", "codex", "opencode"])
def test_captured_environment_is_used_for_preparation_readers_and_accounts(
    tmp_path, monkeypatch, runtime
):
    rt = RUNTIMES[runtime]
    monkeypatch.setenv("TERM", "daemon-term")
    monkeypatch.setenv("COLORTERM", "daemon-color")
    a, b = (rt.capture_environment(tmp_path / name) for name in ("a", "b"))
    for env in (a, b):
        assert "TERM" not in env and "COLORTERM" not in env
        for key in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "TMPDIR"):
            Path(env[key]).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", b["HOME"])
    monkeypatch.setenv("CODEX_HOME", str(b.root / "codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(b.root / "claude"))
    monkeypatch.setenv("XDG_DATA_HOME", b["XDG_DATA_HOME"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-test-value")
    monkeypatch.setenv("OPENCODE_SERVER", "http://example.invalid")
    install_native_environments({runtime: a})
    project = tmp_path / "project"
    project.mkdir()
    rt.pre_trust(project)
    dirs = rt.user_skill_dirs({})
    assert dirs and all(path.is_relative_to(a.root) for path in dirs)
    assert rt.user_instructions({}) == []
    assert rt.usage_paths("session-test", {}) == []
    for action in ("status", "login", "logout"):
        command, environment = rt.account_invocation(action)
        assert command[0] == a.executable
        assert environment == a
        assert "ANTHROPIC_API_KEY" not in environment
        assert "OPENCODE_SERVER" not in environment
    if runtime == "claude":
        recorded = json.loads((a.root / "claude/.claude.json").read_text())
        assert recorded["projects"][str(project.resolve())]["hasTrustDialogAccepted"] is True
        assert rt.hook_settings_path(None) == a.root / "claude/settings.json"
    if runtime == "codex":
        assert (a.root / "codex/config.toml").is_file()
        assert rt.hook_settings_path(None) == a.root / "codex/hooks.json"
    assert not list(b.root.rglob("*.json"))
    assert not list(b.root.rglob("*.toml"))
    with pytest.raises(ValueError, match="already pinned"):
        install_native_environments({runtime: b})
    for key in ("HOME", "CODEX_HOME", "OPENCODE_DB", "AWS_SHARED_CREDENTIALS_FILE"):
        with pytest.raises(ValueError, match="overrides"):
            rt.environment({key: str(b.root)})
    with pytest.raises(ValueError, match="escapes"):
        rt.check_native_path(b.root / "history")


@pytest.mark.parametrize("runtime", ["claude", "codex", "opencode"])
def test_version_and_fresh_resume_commands_use_the_captured_binary(tmp_path, monkeypatch, runtime):
    rt = RUNTIMES[runtime]
    captured = rt.capture_environment(tmp_path / "a")
    install_native_environments({runtime: captured})
    monkeypatch.setattr(
        "agent_backbone.services.runtimes.base.resolve_command", lambda _: "/outside/binary"
    )
    with patch("agent_backbone.services.runtimes.base.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = "2.1.4"
        assert rt.installed_version() == "2.1.4"
        assert run.call_args.args[0] == [captured.executable, "--version"]
        assert run.call_args.kwargs["env"] == dict(captured)
        assert run.call_args.kwargs["cwd"] == captured.root
        for resume in (False, "session-test"):
            command = rt.build_command(resume=resume)
            assert captured.executable in command
            assert "/outside/binary" not in command
            if resume:
                assert "session-test" in " ".join(command)


def test_context_is_immutable_and_rechecks_store_symlinks(tmp_path):
    rt = RUNTIMES["codex"]
    a = rt.capture_environment(tmp_path / "a")
    install_native_environments({rt.id: a})
    with pytest.raises(TypeError):
        a.variables["HOME"] = "elsewhere"
    a.root.mkdir()
    (tmp_path / "b").mkdir()
    (a.root / "codex").symlink_to(tmp_path / "b", target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        rt.usage_paths("session-test", {})
    with pytest.raises(ValueError, match="captured"):
        RUNTIMES["opencode"].environment()


@pytest.mark.parametrize("runtime", ["gemini", "deepcode", "aider"])
def test_unimplemented_native_context_refuses_without_host_writes(tmp_path, runtime):
    with pytest.raises(ValueError, match="unavailable"):
        RUNTIMES[runtime].capture_environment(tmp_path / runtime)
    assert not (tmp_path / runtime).exists()


def test_legacy_environment_and_account_refusal_are_explicit(monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "/legacy/codex")
    rt = RUNTIMES["codex"]
    assert rt.environment({"CUSTOM": "value"}) == {"CUSTOM": "value"}
    assert Path("/legacy/codex/skills") in rt.user_skill_dirs({})
    with pytest.raises(ValueError, match="requires a captured"):
        rt.account_invocation("logout")


@pytest.mark.parametrize(
    "runtime,relative",
    [
        ("claude", "claude/.claude.json"),
        ("codex", "codex/config.toml"),
    ],
)
def test_trust_preparation_refuses_a_config_file_symlink(tmp_path, runtime, relative):
    rt = RUNTIMES[runtime]
    captured = rt.capture_environment(tmp_path / "a")
    install_native_environments({runtime: captured})
    target = captured.root / relative
    target.parent.mkdir(parents=True)
    outside = tmp_path / "outside-config"
    outside.write_text('{"outside": "account-state"}')
    target.symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        rt.pre_trust(tmp_path)
    assert target.is_symlink()
    assert outside.read_text() == '{"outside": "account-state"}'


@pytest.mark.parametrize("name", ["", "BAD=NAME", "BAD\0NAME", 1])
def test_captured_environment_rejects_invalid_variable_names(tmp_path, name):
    from dataclasses import replace

    captured = RUNTIMES["codex"].capture_environment(tmp_path / "a")
    with pytest.raises(ValueError, match="variable name"):
        replace(captured, variables={**captured, name: "value"})


@pytest.mark.parametrize("runtime", ["claude", "codex", "opencode"])
def test_missing_captured_binary_never_falls_back_to_host(tmp_path, monkeypatch, runtime):
    from dataclasses import replace

    rt = RUNTIMES[runtime]
    captured = replace(rt.capture_environment(tmp_path / "native"), executable=None)
    host = tmp_path / "host"
    binary = host / ".opencode/bin/opencode"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv("HOME", str(host))
    install_native_environments({runtime: captured})
    with patch("agent_backbone.services.runtimes.base.subprocess.run") as run:
        assert rt.resolve_binary() is None
        assert not rt.available()
        assert rt.installed_version() is None
        with pytest.raises(RuntimeError, match="binary not found"):
            rt.build_command()
        run.assert_not_called()


@pytest.mark.parametrize("runtime", ["claude", "codex", "opencode"])
@pytest.mark.parametrize("alias", ["symlink", "hardlink", "fifo"])
def test_native_store_aliases_refuse_accounts_and_launch_without_touching_sibling(
    tmp_path, runtime, alias
):
    rt = RUNTIMES[runtime]
    captured = rt.capture_environment(tmp_path / "a")
    sibling = tmp_path / "b" / "account"
    sibling.parent.mkdir()
    sibling.write_text("sibling account sentinel")
    install_native_environments({runtime: captured})
    for relative in rt.native_files:
        path = captured.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if alias == "symlink":
            path.symlink_to(sibling)
        elif alias == "hardlink":
            os.link(sibling, path)
        else:
            os.mkfifo(path)
        with (
            patch("agent_backbone.services.runtimes.base.subprocess.run") as run,
            patch.object(Path, "open", side_effect=AssertionError("unexpected file read")) as read,
        ):
            with patch.object(_environment, "_CONTEXT", None):
                with pytest.raises(ValueError, match="escapes|regular file"):
                    rt.capture_environment(captured.root)
            for action in ("status", "login", "logout"):
                with pytest.raises(ValueError, match="escapes|regular file"):
                    rt.account_invocation(action)
            for resume in (False, "session-test"):
                with pytest.raises(ValueError, match="escapes|regular file"):
                    rt.build_command(resume=resume)
            run.assert_not_called()
            read.assert_not_called()
        path.unlink()
        assert sibling.read_text() == "sibling account sentinel"
    assert not list(sibling.parent.glob("*.json"))


@pytest.mark.parametrize("runtime", ["claude", "codex", "opencode"])
def test_native_file_checks_survive_overlay_and_allow_private_store_updates(tmp_path, runtime):
    rt = RUNTIMES[runtime]
    captured = rt.capture_environment(tmp_path / "a")
    assert not captured.root.exists()  # Capture never creates account storage.
    overlaid = captured.overlay({"TERM": "xterm"})
    for relative in rt.native_files:
        path = captured.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private store")
    overlaid.check()
    # Native writes can replace a file; the snapshot pins paths, not inodes.
    target = captured.root / rt.native_files[0]
    target.unlink()
    target.write_text("updated private store")
    overlaid.check()
    target.unlink()
    target.symlink_to(tmp_path / "missing-sibling-store")
    with pytest.raises(ValueError, match="escapes"):
        overlaid.check()


def test_legacy_account_file_alias_does_not_enable_native_context_checks(tmp_path, monkeypatch):
    home = tmp_path / "legacy"
    home.mkdir()
    (home / "auth.json").symlink_to(tmp_path / "external-account")
    monkeypatch.setenv("CODEX_HOME", str(home))
    assert RUNTIMES["codex"].environment({"CUSTOM": "value"}) == {"CUSTOM": "value"}


@pytest.mark.parametrize("condition", ["parent_file", "loop", "denied"])
def test_unavailable_native_store_is_a_bounded_refusal(tmp_path, monkeypatch, condition):
    rt = RUNTIMES["opencode"]
    captured = rt.capture_environment(tmp_path / "a")
    install_native_environments({rt.id: captured})
    target = captured.root / "data/opencode/auth.json"
    target.parent.parent.mkdir(parents=True)
    if condition == "parent_file":
        target.parent.write_text("not a directory")
    else:
        target.parent.mkdir()
        if condition == "loop":
            target.symlink_to(target)
        else:
            original = Path.stat

            def denied(path, *args, **kwargs):
                if path == target:
                    raise PermissionError("sensitive filesystem diagnostic")
                return original(path, *args, **kwargs)

            monkeypatch.setattr(Path, "stat", denied)
    for operation in (lambda: rt.account_invocation("status"), rt.installed_version):
        with pytest.raises(ValueError, match="^Native (store|path) is unavailable$"):
            operation()


def test_an_uncaptured_runtime_is_unavailable_and_never_the_hosts(tmp_path):
    captured = RUNTIMES["shell"].capture_environment(tmp_path / "shell")
    captured.create_directories()
    install_native_environments({"shell": captured})
    for runtime in ("opencode", "gemini"):
        assert RUNTIMES[runtime].resolve_binary() is None
        assert RUNTIMES[runtime].available() is False
