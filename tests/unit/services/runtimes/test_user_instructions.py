"""User-level instruction files each CLI adds to every session (#274)."""

from pathlib import Path

import pytest

from agent_backbone.services.runtimes import RUNTIMES


@pytest.fixture
def home(monkeypatch):
    """The empty home ``conftest`` gives every test."""
    for key in ("OPENCODE_DISABLE_CLAUDE_CODE", "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT"):
        monkeypatch.delenv(key, raising=False)
    return Path.home()


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
    _write(path, " " * 100_000 + "Always answer in French.\n")  # past the first chunk
    assert RUNTIMES[runtime].user_instructions({}) == [path]


def test_the_agents_own_environment_moves_the_codex_home(home):
    moved = _write(home / "elsewhere" / "AGENTS.md")
    assert RUNTIMES["codex"].user_instructions({"CODEX_HOME": str(moved.parent)}) == [moved]


@pytest.mark.parametrize(
    ("runtime", "relative"),
    [
        ("claude", ".claude/CLAUDE.md"),
        ("codex", ".codex/AGENTS.md"),
        ("gemini", ".gemini/GEMINI.md"),
        ("opencode", ".config/opencode/AGENTS.md"),
        ("deepcode", ".deepcode/AGENTS.md"),
    ],
)
def test_the_agents_own_home_is_the_one_searched(home, tmp_path, runtime, relative):
    path = _write(tmp_path / "agent-home" / relative)
    assert RUNTIMES[runtime].user_instructions({}) == []
    assert RUNTIMES[runtime].user_instructions({"HOME": str(tmp_path / "agent-home")}) == [path]


@pytest.mark.parametrize("blank", ["\u00a0\u3000\n", "\ufeff \n"])
def test_unicode_whitespace_and_a_byte_order_mark_are_empty(home, blank):
    _write(home / ".codex/AGENTS.md", blank)
    assert RUNTIMES["codex"].user_instructions({}) == []


def test_codex_reads_the_override_first_and_skips_an_empty_one(home):
    agents = _write(home / ".codex/AGENTS.md")
    override = _write(home / ".codex/AGENTS.override.md", "")
    assert RUNTIMES["codex"].user_instructions({}) == [agents]
    _write(override)
    assert RUNTIMES["codex"].user_instructions({}) == [override]


def test_gemini_reads_its_configured_context_file_names_and_its_default(home):
    default = _write(home / ".gemini/GEMINI.md")
    agents = _write(home / ".gemini/AGENTS.md")
    settings = '{\n  // AGENTS.md, as the project uses\n  "context": {"fileName": ["AGENTS.md"]}\n}'
    _write(home / ".gemini/settings.json", settings)
    assert RUNTIMES["gemini"].user_instructions({}) == [agents, default]


def test_gemini_takes_the_projects_context_file_names_over_the_users(home, tmp_path):
    default = _write(home / ".gemini/GEMINI.md")
    agents = _write(home / ".gemini/AGENTS.md")
    _write(tmp_path / ".gemini/settings.json", '{"context": {"fileName": "AGENTS.md"}}')
    assert RUNTIMES["gemini"].user_instructions({}) == [default]
    assert RUNTIMES["gemini"].user_instructions({}, tmp_path) == [agents, default]
    _write(tmp_path / ".gemini/settings.json", '{"context": {"fileName": []}}')
    _write(home / ".gemini/settings.json", '{"context": {"fileName": "AGENTS.md"}}')
    assert RUNTIMES["gemini"].user_instructions({}, tmp_path) == [default]


def test_opencode_reads_the_first_file_that_exists_even_when_empty(home, monkeypatch):
    claude = _write(home / ".claude/CLAUDE.md")
    assert RUNTIMES["opencode"].user_instructions({}) == [claude]
    monkeypatch.setenv("OPENCODE_DISABLE_CLAUDE_CODE_PROMPT", "true")
    assert RUNTIMES["opencode"].user_instructions({}) == []
    monkeypatch.delenv("OPENCODE_DISABLE_CLAUDE_CODE_PROMPT")
    _write(home / ".config/opencode/AGENTS.md", "")
    assert RUNTIMES["opencode"].user_instructions({}) == []


def test_deep_code_reads_its_own_only_where_the_project_has_none(home, tmp_path):
    own = _write(home / ".deepcode/AGENTS.md")
    assert RUNTIMES["deepcode"].user_instructions({}, tmp_path) == [own]
    _write(tmp_path / "AGENTS.md")
    assert RUNTIMES["deepcode"].user_instructions({}, tmp_path) == []


@pytest.mark.parametrize("runtime", ["aider", "shell"])
def test_runtimes_without_a_user_level_file_report_none(home, runtime):
    _write(home / ".claude/CLAUDE.md")
    assert RUNTIMES[runtime].user_instructions({}) == []
