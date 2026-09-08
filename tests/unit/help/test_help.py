"""Tests for the help topics and the injected agent brief."""

from __future__ import annotations

import agent_backbone.help as help_module
from agent_backbone.help import get_doc, get_topic, list_docs, list_topics
from agent_backbone.templates import render_agent_brief


class TestTopics:
    def test_shipped_topics_listed_with_summaries(self):
        topics = list_topics()
        names = {t["name"] for t in topics}
        assert {"setup", "swarms", "messaging", "agents", "github", "reports"} <= names
        assert all(t["summary"] for t in topics)

    def test_setup_topic_walks_install_to_first_agent(self):
        content = get_topic("setup")
        for step in ("uv tool install", "backbone init", "agent start", "backbone tell"):
            assert step in content


class TestDocs:
    def test_every_docs_page_is_listed_with_a_summary(self):
        # Whatever is under docs/ in the repository (minus the README index)
        # is exactly what an installed package must be able to print.
        from pathlib import Path

        repo_docs = Path(__file__).resolve().parents[3] / "docs"
        expected = {p.stem for p in repo_docs.glob("*.md")} - {"README"}
        assert expected  # the repository's docs/ was found
        pages = list_docs()
        assert {p["name"] for p in pages} == expected
        assert all(p["summary"] for p in pages)

    def test_get_doc_returns_markdown(self):
        assert get_doc("getting-started").startswith("# Getting started")
        assert get_doc("nope") is None
        assert get_doc("../pyproject") is None

    def test_no_docs_dir_means_empty_not_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(help_module, "_DOCS_DIRS", (tmp_path / "missing",))
        assert list_docs() == []
        assert get_doc("getting-started") is None

    def test_get_topic_returns_markdown(self):
        content = get_topic("swarms")
        assert "swarm create" in content

    def test_unknown_and_invalid_topics_are_none(self):
        assert get_topic("nope") is None
        assert get_topic("../secrets") is None

    def test_data_dir_adds_and_overrides_topics(self, tmp_path):
        override = tmp_path / "help-topics"
        override.mkdir()
        (override / "swarms.md").write_text("# custom swarms doc")
        (override / "deploy.md").write_text("# deploying things")

        assert get_topic("swarms", tmp_path) == "# custom swarms doc"
        names = {t["name"] for t in list_topics(tmp_path)}
        assert "deploy" in names and "messaging" in names


class TestAgentBrief:
    def test_brief_fills_facts_and_points_to_help(self):
        brief = render_agent_brief({"agent_name": "orch", "repo": "acme/app"})
        assert "**orch**" in brief
        assert "acme/app" in brief
        assert "backbone help" in brief
        assert "{agent_name}" not in brief

    def test_data_dir_override(self, tmp_path):
        (tmp_path / "agent-brief.md").write_text("hello {agent_name}")
        assert render_agent_brief({"agent_name": "x"}, tmp_path) == "hello x"


def test_instructions_alias_preserves_explicit_help_overrides(tmp_path):
    assert get_topic("instructions", tmp_path) == get_topic("templates", tmp_path)
    for directory in ("help-topics", "help"):
        path = tmp_path / directory / "instructions.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"# Custom instructions playbook in {directory}")
        entries = {row["name"]: row for row in list_topics(tmp_path)}
        assert entries["instructions"]["summary"] == f"Custom instructions playbook in {directory}"
        assert get_topic("instructions", tmp_path) == path.read_text()
    # The canonical directory wins even when a legacy topic also exists.
    assert "in help" in get_topic("instructions", tmp_path)
