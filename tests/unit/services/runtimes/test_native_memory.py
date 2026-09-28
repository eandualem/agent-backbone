"""Each CLI's own memory across sessions, reported while it is on (#292)."""

from pathlib import Path

import pytest

from agent_backbone.services.runtimes import RUNTIMES

_ON = {
    "claude": (".claude/settings.json", '{"autoMemoryEnabled": true}'),
    "codex": (".codex/config.toml", "[features]\nmemories = true\n"),
}
_OFF = {
    "claude": (".claude/settings.json", '{"autoMemoryEnabled": false}'),
    "codex": (".codex/config.toml", "[features]\nmemories = false\n"),
}
_DEFAULT = {"claude": True, "codex": False}  # 2.1.283 and 0.157.1
_PAIR = pytest.mark.parametrize("runtime", ["claude", "codex"])


@pytest.fixture(autouse=True)
def home(monkeypatch):
    """The empty home ``conftest`` gives every test, and none of the switches."""
    for key in (
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
        "CLAUDE_CODE_SIMPLE",
        "CLAUDE_CODE_SAFE_MODE",
        "GEMINI_SYSTEM_MD",
        "GEMINI_PROMPT_OPERATIONALGUIDELINES",
    ):
        monkeypatch.delenv(key, raising=False)
    return Path.home()


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _reported(runtime, env=None, project=None):
    return RUNTIMES[runtime].native_memory(env or {}, project) is not None


def _project(tmp_path, runtime):
    """A git project the CLI reads its project settings from (Codex: once trusted)."""
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    if runtime == "codex":
        trust = f'[projects."{project.resolve()}"]\ntrust_level = "trusted"\n'
        config = Path.home() / ".codex/config.toml"
        _write(config, (config.read_text() if config.exists() else "") + trust)
    return project


@_PAIR
def test_the_users_settings_turn_it_on_and_off(home, runtime):
    assert _reported(runtime) is _DEFAULT[runtime]
    _write(home / _ON[runtime][0], _ON[runtime][1])
    assert _reported(runtime)
    _write(home / _OFF[runtime][0], _OFF[runtime][1])
    assert not _reported(runtime)


@_PAIR
def test_the_projects_settings_win_over_the_users(home, tmp_path, runtime):
    _write(home / _ON[runtime][0], _ON[runtime][1])
    project = _project(tmp_path, runtime)
    _write(project / _OFF[runtime][0], _OFF[runtime][1])
    assert not _reported(runtime, project=project)
    assert _reported(runtime)  # without the project, the user's setting stands


@_PAIR
def test_the_agents_own_home_is_read(home, tmp_path, runtime):
    flipped = _OFF[runtime] if _DEFAULT[runtime] else _ON[runtime]
    _write(tmp_path / "agent-home" / flipped[0], flipped[1])
    assert _reported(runtime, {"HOME": str(tmp_path / "agent-home")}) is not _DEFAULT[runtime]


def test_claude_codes_variables_decide_before_its_settings(home, monkeypatch):
    _write(home / ".claude/settings.json", '{"autoMemoryEnabled": false}')
    assert not _reported("claude")
    assert _reported("claude", {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0"})  # falsy forces it on
    assert _reported(
        "claude", {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "off", "CLAUDE_CODE_SIMPLE": "1"}
    )
    _write(home / ".claude/settings.json", "{}")
    for variable in (
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
        "CLAUDE_CODE_SIMPLE",
        "CLAUDE_CODE_SAFE_MODE",
    ):
        assert not _reported("claude", {variable: "true"})
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")
    assert not _reported("claude")
    assert _reported("claude", {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0"})  # the agent's own wins


def test_a_claude_code_settings_env_applies_over_the_process(home, tmp_path):
    project = tmp_path / "project"
    _write(
        project / ".claude/settings.local.json", '{"env": {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": 1}}'
    )
    assert not _reported("claude", {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0"}, project)


def test_codex_reads_project_settings_only_under_a_trusted_root(home, tmp_path):
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    _write(project / ".codex/config.toml", "[features]\nmemories = true\n")
    session = project / "service"
    session.mkdir()
    assert not _reported("codex", project=session)
    trusted = f'[projects."{session.resolve()}"]\ntrust_level = "trusted"\n'
    _write(home / ".codex/config.toml", trusted)
    assert not _reported("codex", project=session)  # trust belongs to the root
    _write(
        home / ".codex/config.toml", trusted.replace(str(session.resolve()), str(project.resolve()))
    )
    assert _reported("codex", project=session)
    _write(session / ".codex/config.toml", "[features]\nmemories = false\n")
    assert not _reported("codex", project=session)  # the nearer directory wins


def test_codex_trust_on_a_worktrees_main_checkout_covers_the_worktree(home, tmp_path):
    main, worktree = tmp_path / "app", tmp_path / "app-feature"
    _write(main / ".git/worktrees/app-feature/commondir", "../..\n")
    _write(worktree / ".git", f"gitdir: {main}/.git/worktrees/app-feature\n")
    _write(worktree / ".codex/config.toml", "[features]\nmemories = true\n")
    assert not _reported("codex", project=worktree)
    _write(home / ".codex/config.toml", f'[projects."{main.resolve()}"]\ntrust_level = "trusted"\n')
    assert _reported("codex", project=worktree)


def test_the_agents_own_environment_moves_the_codex_home(home, tmp_path):
    _write(tmp_path / "elsewhere/config.toml", "[features]\nmemories = true\n")
    assert _reported("codex", {"CODEX_HOME": str(tmp_path / "elsewhere")})


def test_gemini_cli_keeps_it_in_its_default_system_prompt(home):
    assert _reported("gemini")
    assert _reported("gemini", {"GEMINI_SYSTEM_MD": "false"})
    assert not _reported("gemini", {"GEMINI_SYSTEM_MD": "/prompts/system.md"})
    assert not _reported("gemini", {"GEMINI_PROMPT_OPERATIONALGUIDELINES": "0"})


@pytest.mark.parametrize("runtime", ["opencode", "deepcode", "aider", "shell"])
def test_a_cli_without_its_own_memory_reports_none(home, runtime):
    assert not _reported(runtime)
