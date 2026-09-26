"""User-level instruction files each CLI adds to every session (#274)."""

import pytest

from agent_backbone.services.runtimes import RUNTIMES

_OVERRIDES = (
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "GEMINI_CLI_HOME",
    "XDG_CONFIG_HOME",
    "OPENCODE_DISABLE_CLAUDE_CODE",
    "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT",
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in _OVERRIDES:
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def _write(path, text="Always answer in French.\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.mark.parametrize(
    ("runtime", "relative"),
    [
        ("claude", ".claude/CLAUDE.md"),
        ("claude", ".claude/rules/style/tone.md"),
        ("codex", ".codex/AGENTS.md"),
        ("gemini", ".gemini/GEMINI.md"),
        ("opencode", ".config/opencode/AGENTS.md"),
        ("opencode", ".claude/CLAUDE.md"),
        ("deepcode", ".deepcode/AGENTS.md"),
    ],
)
def test_a_file_with_content_is_reported_and_an_empty_one_is_not(home, runtime, relative):
    path = _write(home / relative, " \n")
    assert RUNTIMES[runtime].user_instructions({}) == []
    _write(path)
    assert RUNTIMES[runtime].user_instructions({}) == [path]


def test_the_agents_own_environment_moves_the_codex_home(home):
    moved = _write(home / "elsewhere" / "AGENTS.md")
    assert RUNTIMES["codex"].user_instructions({"CODEX_HOME": str(moved.parent)}) == [moved]


def test_codex_reads_the_override_first_and_skips_an_empty_one(home):
    agents = _write(home / ".codex/AGENTS.md")
    override = _write(home / ".codex/AGENTS.override.md", "")
    assert RUNTIMES["codex"].user_instructions({}) == [agents]
    _write(override)
    assert RUNTIMES["codex"].user_instructions({}) == [override]


def test_gemini_reads_its_configured_context_file_names(home):
    _write(home / ".gemini/GEMINI.md")
    agents = _write(home / ".gemini/AGENTS.md")
    _write(home / ".gemini/settings.json", '{"context": {"fileName": ["AGENTS.md"]}}')
    assert RUNTIMES["gemini"].user_instructions({}) == [agents]


def test_opencode_reads_the_first_file_that_exists_even_when_empty(home, monkeypatch):
    claude = _write(home / ".claude/CLAUDE.md")
    assert RUNTIMES["opencode"].user_instructions({}) == [claude]
    monkeypatch.setenv("OPENCODE_DISABLE_CLAUDE_CODE_PROMPT", "true")
    assert RUNTIMES["opencode"].user_instructions({}) == []
    monkeypatch.delenv("OPENCODE_DISABLE_CLAUDE_CODE_PROMPT")
    _write(home / ".config/opencode/AGENTS.md", "")
    assert RUNTIMES["opencode"].user_instructions({}) == []


@pytest.mark.parametrize("runtime", ["aider", "shell"])
def test_runtimes_without_a_user_level_file_report_none(home, runtime):
    _write(home / ".claude/CLAUDE.md")
    assert RUNTIMES[runtime].user_instructions({}) == []
