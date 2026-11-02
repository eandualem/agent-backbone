"""A captured native context cannot drift to ambient or sibling account stores."""

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
    a, b = (rt.capture_environment(tmp_path / name) for name in ("a", "b"))
    for env in (a, b):
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
        assert (a.root / "claude/.claude.json").is_file()
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
