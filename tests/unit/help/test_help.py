"""Tests for the help topics and the injected agent brief."""

from __future__ import annotations

from agent_backbone.help import get_topic, list_topics, render_agent_brief


class TestTopics:
    def test_shipped_topics_listed_with_summaries(self):
        topics = list_topics()
        names = {t["name"] for t in topics}
        assert {"swarms", "messaging", "agents", "github"} <= names
        assert all(t["summary"] for t in topics)

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


class TestBriefInjection:
    def test_brief_file_written_and_passed_for_claude(self, tmp_path):
        from agent_backbone.services.infrastructure._agents import agent_brief_file

        path = agent_brief_file("orch", "acme/app", tmp_path)
        assert path is not None and path.read_text().startswith("# agent-backbone environment")
        assert "**orch**" in path.read_text()

    def test_no_brief_for_other_runtimes(self, tmp_path):
        from agent_backbone.services.infrastructure._agents import agent_brief_file

        assert agent_brief_file("orch", "acme/app", tmp_path, runtime="codex") is None
